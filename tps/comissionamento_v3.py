"""Comissionamento v3: ajuste conjunto de PDD, fatores de saída e forma lateral dos perfis. SRS-COM-008.

O v2 só via o eixo central (PDD e saídas). A forma lateral — horn, ombro e dose fora do campo — ficava sem restrição:
contra o Monaco o modelo v2 dava dose fora do campo alta nos campos pequenos (+1%) e baixa nos grandes (−3,6% no
30×30). O v3 ajusta de uma vez, por mínimos quadrados (Gauss–Newton com diferenças finitas):

  • PDD medidos (vários campos, normalizados a 10 cm; build-up com peso menor);
  • Scp medido em dmax (SSD 100) e Scp·TMR no isocentro (10 e 20 cm), relativos ao 10×10;
  • perfis de referência de outro TPS comissionado (ex.: Monaco), normalizados no eixo, SÓ dentro do campo
    (até 10 mm da borda) e fora dele (a partir de 10 mm da borda). A penumbra fica de fora: ela é do σ da fonte,
    ajustado à parte (tools/ajustar_penumbra_monaco.py) contra o MC.

Parâmetros: endurecimento k, escala do espalhamento s, contaminação (amplitude, σ_e), extrafocal (w, σ), horn
(c1, c2) e transmissões (MLC e mandíbula). A calibração absoluta é refeita depois (comissionamento_v2.recalibrar).
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator
from scipy.optimize import least_squares

from tps import commissioning as C
from tps.comissionamento_v2 import PROF_SAD, _sad_raw
from tps.engine.calc import compute_dose
from tps.engine.phantom import square_field, water_box
from tps.physics.kernels import cones_do_modelo, load_mono
from tps.planejamento.um_tabela import TabelasFeixe

PARAMS = [  # nome, mínimo, máximo, passo relativo da derivada
    ("spectrum_hardening_k", -10.0, 60.0, 1.0),
    ("escala_espalhamento", 0.5, 1.6, 0.02),
    ("e_contam_amp", 0.0, 1.0, 0.03),
    ("e_contam_sigma_mm", 0.0, 150.0, 3.0),
    ("w_extrafocal", 0.0, 0.25, 0.01),
    ("sigma_extrafocal_mm", 5.0, 150.0, 4.0),
    ("horn_c1", -0.05, 0.05, 0.002),
    ("horn_c2", -0.01, 0.01, 0.0002),
    ("mlc_transmission", 0.0, 0.04, 0.002),
    ("jaw_transmission", 0.0, 0.04, 0.002),
]
PROFS_PERFIL = (15.0, 50.0, 100.0, 200.0, 300.0)


def _get(p, nome):
    if nome == "horn_c1":
        return float((p.horn or [0, 0, 0])[0])
    if nome == "horn_c2":
        return float((p.horn or [0, 0, 0])[1])
    return float(getattr(p, nome))


def _set(p, valores: dict):
    horn = list(p.horn or [0.0, 0.0, 0.0]) + [0.0] * 3
    kw = {}
    for k, v in valores.items():
        if k == "horn_c1":
            horn[0] = float(v)
        elif k == "horn_c2":
            horn[1] = float(v)
        else:
            kw[k] = float(v)
    return replace(p, horn=horn[:3], **kw)


class ReferenciaPerfis:
    """Perfis de um TPS de referência (RTDOSE por campo) em água, SSD 100, isocentro na superfície."""

    @staticmethod
    def de_pasta(pasta: Path, alg: str = "CC"):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from comparar_monaco import dose_monaco, ler_pasta
        ref = {}
        for c in ler_pasta(Path(pasta)):
            fs = (c["x"][1] - c["x"][0]) / 10
            if c["alg"] != alg or abs(c["gantry"]) > 0.1 or abs(c["ssd"] - 1000) > 2 or fs > 35:
                continue
            if abs(c["x"][0] + c["x"][1]) > 0.5 or abs((c["y"][1] - c["y"][0]) / 10 - fs) > 0.05:
                continue
            dm, (zm, ym, xm) = dose_monaco(c["rd"])
            iso = c["iso"]
            zc, yc, xc = zm - iso[2], ym - iso[1], xm - iso[0]
            it = RegularGridInterpolator((zc, yc, xc), dm, bounds_error=False, fill_value=np.nan)
            linhas = []
            for d in PROFS_PERFIL:
                for eixo in "XY":
                    meia = fs * 5 * (1000 + d) / 1000
                    pos = xc if eixo == "X" else zc
                    pos = pos[np.abs(pos) <= 190.0]
                    sel = (np.abs(pos) <= meia - 10) | ((np.abs(pos) >= meia + 10) & (np.abs(pos) <= meia + 150))
                    if meia + 10 > 190 and not (np.abs(pos) <= meia - 10).any():
                        continue
                    q = np.c_[np.zeros_like(pos), np.full_like(pos, d), pos] if eixo == "X" else \
                        np.c_[pos, np.full_like(pos, d), np.zeros_like(pos)]
                    v = it(q)
                    eixo0 = it(np.array([[0.0, d, 0.0]]))[0]
                    ok = sel & np.isfinite(v)
                    if ok.sum() > 3 and np.isfinite(eixo0):
                        linhas.append((eixo, d, pos[ok], v[ok] / eixo0 * 100, np.abs(pos[ok]) <= meia - 10))
            ref[round(fs, 1)] = {"linhas": linhas, "passo": float(abs(xc[1] - xc[0]))}
        return ref


def refinar_v3(p, beamdir: Path, kernels: Path, ref_perfis: dict, progress=print, max_nfev: int = 6,
               peso_perfil_dentro: float = 1.0, peso_perfil_fora: float = 1.5, peso_saida: float = 3.0,
               voxel_mm: float = 2.5):
    beamdir = Path(beamdir)
    m = C.measured(beamdir)
    pdd_m, scp_m, dmax = m["pdd"], m["scp"], m["dmax_mm"]
    tab = TabelasFeixe.ler(beamdir, 1.0, dmax)
    mono = load_mono(kernels)
    campos = sorted(set(f for f in (3.0, 5.0, 10.0, 20.0, 30.0) if f in pdd_m) | set(ref_perfis))
    campos_sad = [f for f in (5.0, 10.0, 20.0) if f in scp_m]
    alvo_sad = {(f, d): scp_m[f] * tab.tmr_em(f, d) / (scp_m[10.0] * tab.tmr_em(10.0, 100.0))
                for f in campos_sad for d in PROF_SAD}
    cache = {}

    def cones(pp):
        k = (round(pp.spectrum_hardening_k, 4), round(pp.escala_espalhamento, 5))
        if k not in cache:
            cache[k] = cones_do_modelo(mono, pp)
        return cache[k]

    def calcula(pp, fs):
        lado = min(410.0, max(200.0, fs * 10 * 1.3 + 160.0))
        prof = 310.0 if fs in ref_perfis else 300.0
        g = water_box(lado, prof, voxel_mm)
        d = compute_dose(g, [square_field(fs, fs, mu=1.0)], pp, cones(pp))
        nz, ny, nx = d.shape
        ax = tuple(g.origin_mm[i] + np.arange(n) * voxel_mm for i, n in ((2, nz), (1, ny), (0, nx)))
        return RegularGridInterpolator(ax, d, bounds_error=False, fill_value=0.0)

    nomes = [n for n, *_ in PARAMS]

    def residuos(x, detalhe=False):
        pp = _set(p, dict(zip(nomes, x)))
        partes = {"pdd": [], "scp": [], "sad": [], "perfil_dentro": [], "perfil_fora": []}
        cax = {}
        for fs in campos:
            it = calcula(pp, fs)
            prof = np.arange(0.0, 300.0, 1.0)
            dd = it(np.c_[np.zeros_like(prof), prof, np.zeros_like(prof)])
            cax[fs] = float(np.interp(dmax, prof, dd))
            if fs in pdd_m:
                mm = pdd_m[fs]
                sel = (mm["x"] >= 5.0) & (mm["x"] <= 295.0)
                xx = mm["x"][sel][::max(1, int(sel.sum() // 60))]
                calc = np.interp(xx, prof, dd) / np.interp(100.0, prof, dd)
                meas = np.interp(xx, mm["x"], mm["y"]) / np.interp(100.0, mm["x"], mm["y"])
                partes["pdd"].append((calc - meas) * 100 * np.where(xx < dmax, 0.3, 1.0))
            if fs in ref_perfis:
                passo = ref_perfis[fs]["passo"]
                caixa = np.linspace(-passo / 2, passo / 2, 5)
                for eixo, d, pos, val, dentro in ref_perfis[fs]["linhas"]:
                    acc = 0.0
                    for o in caixa:
                        q = pos + o
                        pts = np.c_[np.zeros_like(q), np.full_like(q, d), q] if eixo == "X" else \
                            np.c_[q, np.full_like(q, d), np.zeros_like(q)]
                        acc = acc + it(pts)
                    nos = acc / len(caixa) / float(it(np.array([[0.0, d, 0.0]]))[0]) * 100
                    e = nos - val
                    partes["perfil_dentro"].append(e[dentro] * peso_perfil_dentro / np.sqrt(max(1, dentro.sum() / 15)))
                    partes["perfil_fora"].append(e[~dentro] * peso_perfil_fora / np.sqrt(max(1, (~dentro).sum() / 15)))
        partes["scp"] = [np.array([(cax[f] / cax[10.0] - scp_m[f]) / scp_m[f] * 100 * peso_saida
                                   for f in cax if f in scp_m and f != 10.0])]
        c = cones(pp)
        r10 = _sad_raw(pp, c, 10.0, 100.0)
        partes["sad"] = [np.array([(_sad_raw(pp, c, f, d) / r10 - alvo_sad[(f, d)]) / alvo_sad[(f, d)] * 100 * peso_saida
                                   for f in campos_sad for d in PROF_SAD if not (f == 10.0 and d == 100.0)])]
        r = {k: np.concatenate(v) if v else np.zeros(0) for k, v in partes.items()}
        if detalhe:
            return r
        return np.concatenate(list(r.values()))

    x0 = np.array([_get(p, n) for n in nomes])
    lo = np.array([a for _, a, _, _ in PARAMS])
    hi = np.array([b for _, _, b, _ in PARAMS])
    x0 = np.clip(x0, lo + 1e-9, hi - 1e-9)
    passos = np.array([s for *_, s in PARAMS])
    t0 = time.time()
    r0 = residuos(x0, detalhe=True)
    progress(f"v3 início: " + "  ".join(f"{k} {np.sqrt(np.mean(v ** 2)):.2f}" for k, v in r0.items() if len(v))
             + f"  ({time.time() - t0:.0f} s por avaliação)")
    ultimo = {"x": x0}

    def fun(x):
        ultimo["x"] = x
        return residuos(x)

    res = least_squares(fun, x0, bounds=(lo, hi), diff_step=passos / np.maximum(np.abs(x0), passos),
                        x_scale=passos * 5, max_nfev=max_nfev, method="trf", verbose=0)
    pf = _set(p, dict(zip(nomes, res.x)))
    r1 = residuos(res.x, detalhe=True)
    log = {"inicio": {k: float(np.sqrt(np.mean(v ** 2))) for k, v in r0.items() if len(v)},
           "final": {k: float(np.sqrt(np.mean(v ** 2))) for k, v in r1.items() if len(v)},
           "parametros": dict(zip(nomes, map(float, res.x))), "avaliacoes": int(res.nfev)}
    progress("v3 final: " + "  ".join(f"{k} {v:.2f}" for k, v in log["final"].items()))
    progress("parâmetros: " + ", ".join(f"{k}={v:.4g}" for k, v in log["parametros"].items()))
    return pf, log
