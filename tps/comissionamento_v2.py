"""Comissionamento v2 do modelo de feixe. SRS-COM-006/007.

O v1 (herdado do SecDose) ajusta o espectro só ao PDD 10×10 e a fonte extrafocal só ao Scp em dmax a SSD 100. Isso
deixa o espalhamento no fantoma sem restrição: o modelo erra até 5% em geometria isocêntrica e até 5% logo após dmax
nos campos grandes (Synergy e Precise). O v2 parte do v1 e refina em duas etapas alternadas:

  A. Forma do PDD de vários campos (3×3 a 30×30, normalizados a 10 cm, build-up com peso menor):
       endurecimento do espectro k, escala do espalhamento no fantoma s, contaminação eletrônica (amplitude e
       largura lateral σ_e — a contaminação passa a crescer com o campo).
  B. Fatores de saída em duas geometrias: Scp em dmax a SSD 100 e dose relativa no isocentro a 10 e 20 cm
       (Scp·TMR das tabelas, relativo ao 10×10 a 10 cm): fonte extrafocal (w, σ) e s.

Em seguida recalibra a dose absoluta como no v1. Tudo o que o v2 muda fica registrado no log.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

from tps import commissioning as C
from tps.engine.calc import compute_dose
from tps.engine.phantom import cax_profile, square_field, water_box
from tps.physics.kernels import cones_do_modelo, load_mono
from tps.planejamento.um_tabela import TabelasFeixe

CAMPOS_PDD = (3.0, 5.0, 10.0, 20.0, 30.0)
CAMPOS_SAD = (5.0, 10.0, 20.0)
PROF_SAD = (100.0, 200.0)


def _pdd_raw(p, cones, fs):
    y, d = C.calc_pdd(p, cones, fs)
    return y, d


def _sad_raw(p, cones, fs, prof):
    vox = 2.5 if fs <= 6 else 5.0
    g = water_box(max(200.0, fs * 12.5 + 60.0), prof + 60.0 + vox, vox)
    d = compute_dose(g, [square_field(fs, fs, ssd_mm=1000.0 - prof, mu=1.0)], p, cones)
    y, c = cax_profile(d, g)
    return float(np.interp(prof, y, c))


def refinar(p, beamdir: Path, kernels: Path, progress=print, max_iter_a: int = 50, max_iter_b: int = 35,
            rodadas: int = 2) -> tuple:
    """Refina um modelo do v1 (já calibrado). Devolve (modelo, log)."""
    beamdir = Path(beamdir)
    m = C.measured(beamdir)
    pdd_m, scp_m, dmax = m["pdd"], m["scp"], m["dmax_mm"]
    tab = TabelasFeixe.ler(beamdir, 1.0, dmax)
    mono = load_mono(kernels)
    campos_pdd = [f for f in CAMPOS_PDD if f in pdd_m]
    campos_sad = [f for f in CAMPOS_SAD if f in scp_m]
    alvo_sad = {(f, d): scp_m[f] * tab.tmr_em(f, d) / (scp_m[10.0] * tab.tmr_em(10.0, 100.0))
                for f in campos_sad for d in PROF_SAD}
    cache = {}

    def cones(pp):
        k = (round(pp.spectrum_hardening_k, 3), round(pp.escala_espalhamento, 4))
        if k not in cache:
            cache[k] = cones_do_modelo(mono, pp)
        return cache[k]

    # contaminação ativa durante o ajuste: escala fixa (a amplitude absorve)
    if p.e_contam_ref_raw <= 0:
        p = replace(p, e_contam_ref_raw=1.0)
    p = replace(p, e_contam_sigma_mm=p.e_contam_sigma_mm or 30.0)

    def erro_pdd(pp):
        errs = []
        for f in campos_pdd:
            y, d = _pdd_raw(pp, cones(pp), f)
            mm = pdd_m[f]
            sel = mm["x"] >= 5.0
            x = mm["x"][sel]
            calc = np.interp(x, y, d) / np.interp(100.0, y, d)
            meas = mm["y"][sel] / np.interp(100.0, mm["x"], mm["y"])
            w = np.where(x < dmax, 0.3, 1.0)
            errs.append((calc - meas) * 100 * w)
        return np.concatenate(errs)

    def erro_saida(pp):
        c = cones(pp)
        dm = {f: float(np.interp(dmax, *_pdd_raw(pp, c, f))) for f in sorted(set(campos_pdd) | {10.0})
              if f in scp_m}
        e_scp = [(dm[f] / dm[10.0] - scp_m[f]) * 100 for f in dm]
        ref = _sad_raw(pp, c, 10.0, 100.0)
        e_sad = [(_sad_raw(pp, c, f, d) / ref - alvo_sad[(f, d)]) / alvo_sad[(f, d)] * 100
                 for f in campos_sad for d in PROF_SAD if not (f == 10.0 and d == 100.0)]
        return np.array(e_scp + e_sad)

    log = {"inicio": {"k": p.spectrum_hardening_k, "s": p.escala_espalhamento, "w_ef": p.w_extrafocal}}

    def comp_a(x, base):
        return replace(base, spectrum_hardening_k=float(x[0]), escala_espalhamento=float(np.clip(x[1], 0.6, 1.6)),
                       e_contam_amp=float(np.clip(x[2], 0, 0.5)), e_contam_sigma_mm=float(np.clip(x[3], 0, 150)))

    def comp_b(x, base):
        return replace(base, w_extrafocal=float(np.clip(x[0], 0, 0.25)),
                       sigma_extrafocal_mm=float(np.clip(x[1], 5, 120)),
                       escala_espalhamento=float(np.clip(x[2], 0.6, 1.6)))

    for rodada in range(1, rodadas + 1):
        progress(f"v2 rodada {rodada}: forma do PDD ({len(campos_pdd)} campos)")
        fa = lambda x, b=p: float(np.sqrt(np.mean(erro_pdd(comp_a(x, b)) ** 2)))   # noqa: E731
        x0 = [p.spectrum_hardening_k, p.escala_espalhamento, max(p.e_contam_amp, 0.02), p.e_contam_sigma_mm]
        r = minimize(fa, x0, method="Nelder-Mead",
                     options={"maxiter": max_iter_a, "xatol": 0.01, "fatol": 0.005,
                              "initial_simplex": np.vstack([x0, np.array(x0) + np.diag([2.0, 0.1, 0.02, 15.0])])})
        p = comp_a(r.x, p)
        log[f"A{rodada}"] = {"rms_pdd_pct": float(r.fun), "k": p.spectrum_hardening_k, "s": p.escala_espalhamento,
                             "e_amp": p.e_contam_amp, "e_sigma_mm": p.e_contam_sigma_mm}
        progress(f"  rms PDD {r.fun:.2f}% (k={p.spectrum_hardening_k:.2f}, s={p.escala_espalhamento:.3f})")
        progress(f"v2 rodada {rodada}: fatores de saída em dmax e no isocentro")
        fb = lambda x, b=p: float(np.sqrt(np.mean(erro_saida(comp_b(x, b)) ** 2)))  # noqa: E731
        x0 = [p.w_extrafocal, p.sigma_extrafocal_mm, p.escala_espalhamento]
        r = minimize(fb, x0, method="Nelder-Mead",
                     options={"maxiter": max_iter_b, "xatol": 0.002, "fatol": 0.005,
                              "initial_simplex": np.vstack([x0, np.array(x0) + np.diag([0.02, 10.0, 0.08])])})
        p = comp_b(r.x, p)
        log[f"B{rodada}"] = {"rms_saida_pct": float(r.fun), "w_ef": p.w_extrafocal, "sigma_ef": p.sigma_extrafocal_mm,
                             "s": p.escala_espalhamento}
        progress(f"  rms saídas {r.fun:.2f}% (w={p.w_extrafocal:.3f}, σ={p.sigma_extrafocal_mm:.0f}, "
                 f"s={p.escala_espalhamento:.3f})")
    log["erros_finais"] = {"pdd": erro_pdd(p).round(2).tolist(), "saidas": erro_saida(p).round(2).tolist()}
    return p, log


def recalibrar(p, beamdir: Path, cgy_por_mu: float, kernels: Path):
    """Mesma calibração absoluta do v1 (âncora: PDD 10×10 a 10 cm × cGy/UM em dmax), com os cones do modelo."""
    m = C.measured(Path(beamdir))
    dmax = m["dmax_mm"]
    c = cones_do_modelo(load_mono(kernels), p)
    y, d = C.calc_pdd(replace(p, norm_dose_per_mu=1.0), c, 10.0)
    raw10 = float(np.interp(100.0, y, d))
    pdd10 = float(np.interp(100.0, m["pdd"][10.0]["x"], m["pdd"][10.0]["y"]))
    ref = dict(p.ref or {})
    ref.update({"campo_cm": 10, "ssd_mm": 1000, "prof_calibracao_dmax_mm": dmax, "cGy_por_MU_dmax": cgy_por_mu,
                "ancora_prof_mm": 100.0, "pdd_ancora": pdd10, "comissionamento": "v2"})
    return replace(p, norm_dose_per_mu=cgy_por_mu * pdd10 / 100.0 / 100.0 / raw10, comissionado=True,
                   demonstracao=False, ref=ref)
