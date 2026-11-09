"""Comissionamento e validação do modelo de feixe contra dados medidos em água. SRS-COM-001..010.

Etapas do ajuste:
 1. Espectro: endurecimento k ajustado ao PDD 10×10 (≥ dmax, normalizado em 10 cm).
 2. Contaminação eletrônica: amplitude/decaimento na região de buildup (10×10).
 3. Fonte extrafocal: peso e largura ajustados aos fatores Scp (dmax, SSD 100).
 4. Perfil radial (horns): ajustado à razão off-axis diagonal 40×40 (se houver).
 5. Calibração absoluta: ancorada em 10 cm de profundidade no 10×10.
Depois, validação contra todos os PDD, Scp e OAR disponíveis (JSON + figuras).
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import replace
from pathlib import Path

import numpy as np
from scipy.optimize import minimize, minimize_scalar

from tps.beamdata.csv_loader import load_curves
from tps.engine.beam_model import BeamModelParams, FluenceGrid, aperture, energy_fluence
from tps.engine.calc import compute_dose
from tps.engine.phantom import cax_profile, square_field, water_box
from tps.physics.kernels import cones_for_spectrum, load_mono
from tps.physics.spectrum import Spectrum

from tps import paths
KERNELS = paths.kernels_file()
VALID_FIELDS = (3.0, 5.0, 10.0, 20.0, 30.0, 40.0)


def parse_energy(key: str) -> tuple[int, bool]:
    """'6MV' → (6, False); '10FFF' → (10, True)."""
    m = re.match(r"^(\d+)\s*(MV|FFF|X|XFFF)?$", key.strip().upper())
    if not m:
        raise ValueError(f"Energia inválida: {key}. Use, por exemplo, 6MV, 10MV, 15MV, 6FFF ou 10FFF.")
    return int(m.group(1)), "FFF" in (m.group(2) or "")


def energy_key(nominal: int, fff: bool) -> str:
    return f"{nominal}FFF" if fff else f"{nominal}MV"


# ------------------------------------------------------------------- cálculo em água
def _box_for(fs_cm: float):
    lateral = max(200.0, fs_cm * 10 * 1.25 + 60.0)
    vox = 2.5 if fs_cm <= 6 else 5.0
    return water_box(lateral, 300.0 + vox, vox)


def calc_pdd(params, cones, fs_cm):
    g = _box_for(fs_cm)
    d = compute_dose(g, [square_field(fs_cm, fs_cm, mu=1.0)], params, cones)
    return cax_profile(d, g)


def calc_field(params, cones, fs_cm, depth_mm: float = 210.0):
    """Campo em água com voxel de 2,5 mm (resolução necessária para a penumbra) até a profundidade pedida."""
    lateral = fs_cm * 10 * 1.3 + 100.0
    g = water_box(lateral, depth_mm + 10.0, 2.5)
    return compute_dose(g, [square_field(fs_cm, fs_cm, mu=1.0)], params, cones), g


def profile_from(dose, grid, depth_mm, eixo):
    """Perfil normalizado no eixo (100%) à profundidade dada: crossplane = x (lâminas), inplane = z (diafragmas)."""
    nz, ny, nx = dose.shape
    y = grid.origin_mm[1] + np.arange(ny) * grid.spacing_mm[1]
    j = int(np.argmin(np.abs(y - depth_mm)))
    if eixo == "crossplane":
        pos = grid.origin_mm[0] + np.arange(nx) * grid.spacing_mm[0]
        prof = 0.5 * (dose[nz // 2, j, :] + dose[nz // 2 - 1, j, :]) if nz % 2 == 0 else dose[nz // 2, j, :]
    else:
        pos = grid.origin_mm[2] + np.arange(nz) * grid.spacing_mm[2]
        prof = 0.5 * (dose[:, j, nx // 2] + dose[:, j, nx // 2 - 1]) if nx % 2 == 0 else dose[:, j, nx // 2]
    return pos, prof / np.interp(0.0, pos, prof) * 100.0


def profile_error(meas, calc_pos, calc_prof, fs_cm, depth_mm):
    """Diferença (pontos de %) na penumbra e fora do campo: |x| ≥ 80% da meia-largura projetada."""
    half = fs_cm * 5.0 * (1000.0 + depth_mm) / 1000.0
    sel = np.abs(meas["x"]) >= 0.8 * half
    if not sel.any():
        return np.array([])
    return np.interp(meas["x"][sel], calc_pos, calc_prof) - meas["y"][sel]


def penumbra_80_20(pos, prof):
    """Largura média 80–20% das duas bordas (mm)."""
    out = []
    for side in (pos >= 0, pos <= 0):
        x, y = pos[side], prof[side]
        o = np.argsort(y)
        try:
            out.append(abs(np.interp(80, y[o], x[o]) - np.interp(20, y[o], x[o])))
        except ValueError:
            pass
    return float(np.mean(out)) if out else float("nan")


def _scp_field(params, cones, fs_cm, depth_mm: float = 150.0):
    """Campo só até ~dmax + 20 cm: retroespalhamento suficiente para o Scp (< 0,1%) e mais rápido que o PDD completo."""
    lateral = max(200.0, fs_cm * 10 * 1.25 + 60.0)
    vox = 2.5 if fs_cm <= 6 else 5.0
    g = water_box(lateral, depth_mm, vox)
    return compute_dose(g, [square_field(fs_cm, fs_cm, mu=1.0)], params, cones), g


def calc_oar_diag(params, cones, depths_mm, fs_cm=40.0):
    g = water_box(560.0, max(260.0, max(depths_mm) + 20), 5.0)
    d = compute_dose(g, [square_field(fs_cm, fs_cm, mu=1.0)], params, cones)
    nz, ny, nx = d.shape
    x = g.origin_mm[0] + np.arange(nx) * g.spacing_mm[0]
    y = g.origin_mm[1] + np.arange(ny) * g.spacing_mm[1]
    out = {}
    for dep in depths_mm:
        j = int(np.argmin(np.abs(y - dep)))
        diag = np.array([d[i, j, i] for i in range(nx)])
        r = x * np.sqrt(2.0)
        out[dep] = (r * 1000.0 / (1000.0 + dep), diag / np.interp(0.0, r, diag))
    return out


# ------------------------------------------------------------------ dados medidos
def measured(beamdir: Path) -> dict:
    """Lê os CSV da pasta (pdd.csv e fatores_output.csv obrigatórios; oar_diagonal.csv opcional)."""
    beamdir = Path(beamdir)
    if not (beamdir / "pdd.csv").exists():
        raise FileNotFoundError("Falta pdd.csv com os PDD medidos (SSD 100 cm)")
    pdd_all = load_curves(beamdir / "pdd.csv", "pdd")
    pdd = {}
    for k, v in pdd_all.items():
        if k[2] == k[3] and abs(k[4] - 100) < 0.5:
            pdd[k[2]] = v
    if 10.0 not in pdd:
        raise ValueError("Os dados precisam ter o PDD do campo 10×10 cm² em SSD 100 cm")
    if not (beamdir / "fatores_output.csv").exists():
        raise FileNotFoundError("Falta fatores_output.csv com os fatores Scp (normalizados no 10×10)")
    with (beamdir / "fatores_output.csv").open(encoding="utf-8-sig") as fh:
        scp = {}
        for r in csv.DictReader(fh):
            if r["campo_x_cm"] == r["campo_y_cm"]:
                scp[float(str(r["campo_x_cm"]).replace(",", "."))] = float(str(r["fator_output"]).replace(",", "."))
    if 10.0 not in scp or abs(scp[10.0] - 1) > 0.01:
        raise ValueError("Os fatores Scp precisam incluir o 10×10 normalizado em 1,000")
    oar = {}
    f = beamdir / "oar_diagonal.csv"
    if f.exists():
        with f.open(encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                oar.setdefault(float(r["profundidade_mm"]), []).append((float(r["posicao_mm"]), float(r["dose_rel"]) / 100))
        oar = {k: np.array(sorted(v)) for k, v in oar.items()}
    perfis = {}
    f = beamdir / "perfis.csv"
    if f.exists():
        for k, v in load_curves(f, "perfis").items():
            # chave: (campo, profundidade_mm, eixo); só campos quadrados em SSD 100
            if k[2] == k[3] and abs(k[4] - 100) < 0.5 and k[6].lower() in ("crossplane", "inplane"):
                y = v["y"] / np.interp(0.0, v["x"], v["y"]) * 100.0
                perfis[(k[2], k[5], k[6].lower())] = {"x": v["x"], "y": y}
    m10 = pdd[10.0]
    dmax = float(m10["x"][int(np.argmax(m10["y"]))])
    return {"pdd": pdd, "scp": scp, "oar": oar, "perfis": perfis, "dmax_mm": dmax}


def summarize(beamdir: Path) -> dict:
    """Resumo legível dos dados disponíveis (para a tela de cadastro)."""
    try:
        m = measured(beamdir)
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    return {"ok": True, "pdd_campos": sorted(m["pdd"]), "scp_campos": sorted(m["scp"]), "oar_prof_mm": sorted(m["oar"]),
            "perfis": sorted({f"{fs:g}×{fs:g} {ax} {d / 10:g} cm" for fs, d, ax in m["perfis"]}),
            "dmax_mm": m["dmax_mm"], "pdd_prof_max_mm": float(max(v["x"].max() for v in m["pdd"].values()))}


def _pdd_err(calc, meas, d_from, norm_mm=100.0):
    y, d = calc
    mx, my = meas["x"], meas["y"]
    sel = (mx >= d_from) & (mx <= 300)
    c = np.interp(mx[sel], y, d)
    c = c / np.interp(norm_mm, y, d) * np.interp(norm_mm, mx, my)
    return my[sel], c


# ------------------------------------------------------------------- ajuste
def commission(beamdir: Path, energy: str, base: BeamModelParams, cgy_por_mu: float,
               kernels: Path = KERNELS, progress=print) -> tuple[BeamModelParams, dict]:
    nominal, fff = parse_energy(energy)
    m = measured(beamdir)
    pdd_m, scp_m, oar_m, dmax = m["pdd"], m["scp"], m["oar"], m["dmax_mm"]
    mono = load_mono(kernels)
    if Spectrum.max_energy(nominal) > mono.energies.max() + 0.5:
        raise ValueError(f"Kernels disponíveis só até {mono.energies.max():g} MeV; gere os de alta energia "
                         f"(tools/gerar_kernels.py) para {nominal} MV")
    p = replace(base, energy_mv=float(nominal), spectrum_hardening_k=-4.0 if fff else 0.0, horn=[0.0, 0.0, 0.0],
                e_contam_amp=0.0, e_contam_ref_raw=0.0, head_leakage=base.head_leakage, norm_dose_per_mu=1.0)
    cache: dict = {}

    def cones_for(pp):
        k = round(pp.spectrum_hardening_k, 3)
        if k not in cache:
            cache[k] = cones_for_spectrum(mono, Spectrum.mohan(nominal, k))
        return cache[k]

    log = {"energia": energy, "dmax_mm": dmax}
    progress("Ajustando o espectro ao PDD 10×10")

    def f_k(k):
        pk = replace(p, spectrum_hardening_k=float(k))
        a, c = _pdd_err(calc_pdd(pk, cones_for(pk), 10.0), pdd_m[10.0], dmax)
        return float(np.sqrt(np.mean((c - a) ** 2)))
    r = minimize_scalar(f_k, bounds=(-25.0, 25.0), method="bounded", options={"xatol": 0.2})
    p = replace(p, spectrum_hardening_k=float(r.x))
    log["espectro"] = {"k": float(r.x), "rms_pdd10x10_pct": float(r.fun)}

    progress("Ajustando a contaminação eletrônica")
    y, d = calc_pdd(p, cones_for(p), 10.0)
    mm = pdd_m[10.0]
    dn = d / np.interp(100.0, y, d) * np.interp(100.0, mm["x"], mm["y"])
    sel = (mm["x"] < dmax) & (mm["x"] > 0)
    best = (0.0, 2.0)
    if sel.sum() >= 2:
        resid = mm["y"][sel] - np.interp(mm["x"][sel], y, dn)
        err0 = np.sum(resid ** 2)
        for kc in np.linspace(0.5, 6.0, 23):
            basis = np.exp(-kc * mm["x"][sel] / 10.0)
            amp = float(np.dot(basis, resid) / np.dot(basis, basis))
            err = float(np.sum((resid - amp * basis) ** 2))
            if amp >= 0 and err < err0:
                best, err0 = (amp, kc), err
    p = replace(p, e_contam_amp=best[0] / 100.0, e_contam_k_cm=best[1])
    log["contaminacao_eletronica"] = {"amp_frac_dmax": best[0] / 100.0, "k_1_por_cm": best[1]}

    perf_m = m["perfis"]
    fs_list = [f for f in VALID_FIELDS if f in scp_m]
    if perf_m and len(fs_list) >= 3:
        campos_p = sorted({k[0] for k in perf_m})
        progress(f"Ajustando fonte, extrafocal e penumbra aos Scp e a {len(perf_m)} perfis medidos")

        def f_joint(x):
            pp = replace(p, sigma_src_mm=float(np.clip(x[0], 0.3, 5)), w_extrafocal=float(np.clip(x[1], 0, 0.2)),
                         sigma_extrafocal_mm=float(np.clip(x[2], 5, 80)))
            cones = cones_for(pp)
            vals, errs = {}, []
            prof_max = max(k[1] for k in perf_m) + 20.0
            for f in sorted(set(fs_list) | set(campos_p)):
                if f in campos_p:
                    d, g = calc_field(pp, cones, f, prof_max)
                else:
                    d, g = None, None
                if f in fs_list:
                    vals[f] = float(np.interp(dmax, *cax_profile(*((d, g) if d is not None else _scp_field(pp, cones, f, dmax + 200.0)))))
                for (fs, dep, ax), meas in perf_m.items():
                    if fs == f:
                        errs.append(profile_error(meas, *profile_from(d, g, dep, ax), fs, dep))
            scp_err = [(vals[f] / vals[10.0] - scp_m[f]) * 100 for f in fs_list]   # em %
            e = np.concatenate([np.asarray(scp_err) * 2.0] + [e for e in errs if e.size])
            return float(np.sqrt(np.mean(e ** 2)))
        r = minimize(f_joint, [1.5, 0.06, 25.0], method="Nelder-Mead", options={"xatol": 0.01, "fatol": 1e-3, "maxiter": 60})
        p = replace(p, sigma_src_mm=float(np.clip(r.x[0], 0.3, 5)), w_extrafocal=float(np.clip(r.x[1], 0, 0.2)),
                    sigma_extrafocal_mm=float(np.clip(r.x[2], 5, 80)))
        log["fonte_e_extrafocal"] = {"sigma_src_mm": p.sigma_src_mm, "w": p.w_extrafocal, "sigma_ef_mm": p.sigma_extrafocal_mm,
                                     "rms_combinado": float(r.fun), "perfis": len(perf_m)}
    elif len(fs_list) >= 3:
        progress(f"Ajustando a fonte extrafocal aos fatores Scp ({len(fs_list)} campos)")

        def out_factors(pp):
            vals = {f: float(np.interp(dmax, *cax_profile(*_scp_field(pp, cones_for(pp), f, dmax + 200.0)))) for f in fs_list}
            return {f: v / vals[10.0] for f, v in vals.items()}

        def f_ef(x):
            pp = replace(p, w_extrafocal=float(np.clip(x[0], 0, 0.2)), sigma_extrafocal_mm=float(np.clip(x[1], 5, 80)))
            of = out_factors(pp)
            return float(np.sqrt(np.mean([(of[f] - scp_m[f]) ** 2 for f in fs_list])))
        r = minimize(f_ef, [0.06, 25.0], method="Nelder-Mead", options={"xatol": 0.002, "fatol": 1e-4, "maxiter": 40})
        p = replace(p, w_extrafocal=float(np.clip(r.x[0], 0, 0.2)), sigma_extrafocal_mm=float(np.clip(r.x[1], 5, 80)))
        log["extrafocal"] = {"w": p.w_extrafocal, "sigma_mm": p.sigma_extrafocal_mm, "rms_scp": float(r.fun)}

    depths = [dp for dp in (50.0, 100.0) if dp in oar_m]
    if depths:
        progress("Ajustando o perfil radial (horns) à razão off-axis")

        def f_h(c):
            pp = replace(p, horn=[float(c[0]), float(c[1]), 0.0])
            calc = calc_oar_diag(pp, cones_for(pp), depths)
            errs = []
            for dep in depths:
                rm, vm = oar_m[dep][:, 0], oar_m[dep][:, 1]
                s = rm <= 170.0
                rc, vc = calc[dep]
                errs.append(np.interp(rm[s], rc[rc >= 0], vc[rc >= 0]) - vm[s])
            return float(np.sqrt(np.mean(np.concatenate(errs) ** 2)))
        r = minimize(f_h, [0.0, 0.0], method="Nelder-Mead", options={"xatol": 1e-4, "fatol": 1e-5, "maxiter": 40})
        p = replace(p, horn=[float(r.x[0]), float(r.x[1]), 0.0])
        log["horns"] = {"c1": p.horn[0], "c2": p.horn[1], "rms_oar": float(r.fun)}

    progress("Calibrando a dose absoluta")
    fg = FluenceGrid()
    sq = square_field(10, 10)
    psi_cax = float(energy_fluence(aperture(fg, None, sq.jaw_y, sq.mlc_a, sq.mlc_b, sq.leaf_boundaries, p), fg, p)[fg.nv // 2, fg.nu // 2])
    p0 = replace(p, norm_dose_per_mu=1.0, e_contam_ref_raw=0.0)
    y, d = calc_pdd(p0, cones_for(p0), 10.0)
    p = replace(p, e_contam_ref_raw=float(np.interp(dmax, y, d)) / psi_cax)
    y, d = calc_pdd(replace(p, norm_dose_per_mu=1.0), cones_for(p), 10.0)
    raw10 = float(np.interp(100.0, y, d))
    pdd10 = float(np.interp(100.0, pdd_m[10.0]["x"], pdd_m[10.0]["y"]))
    p = replace(p, norm_dose_per_mu=cgy_por_mu * pdd10 / 100.0 / 100.0 / raw10, comissionado=True, demonstracao=False,
                ref={"campo_cm": 10, "ssd_mm": 1000, "prof_calibracao_dmax_mm": dmax, "cGy_por_MU_dmax": cgy_por_mu,
                     "ancora_prof_mm": 100.0, "pdd_ancora": pdd10, "fff": fff, "energia": energy})
    return p, log


# ------------------------------------------------------------------- validação
def validate(model: BeamModelParams, beamdir: Path, out_dir: Path, kernels: Path = KERNELS, progress=print) -> dict:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    m = measured(beamdir)
    pdd_m, scp_m, oar_m, dmax = m["pdd"], m["scp"], m["oar"], m["dmax_mm"]
    nominal, _ = parse_energy(model.ref.get("energia", f"{int(model.energy_mv)}MV"))
    cones = cones_for_spectrum(load_mono(kernels), Spectrum.mohan(nominal, model.spectrum_hardening_k))
    res = {"pdd": {}, "scp": {}, "oar": {}, "dmax_mm": dmax}
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.6))
    raw = {}
    fields = [f for f in VALID_FIELDS if f in pdd_m]
    for f in fields:
        progress(f"Validando PDD {f:g}×{f:g}")
        y, d = calc_pdd(model, cones, f)
        raw[f] = float(np.interp(dmax, y, d))
        mm = pdd_m[f]
        c = np.interp(mm["x"], y, d) / np.interp(100.0, y, d) * np.interp(100.0, mm["x"], mm["y"])
        diff = c - mm["y"]
        sel = (mm["x"] >= dmax) & (mm["x"] <= 300)
        res["pdd"][f"{f:g}x{f:g}"] = {"max_abs_diff_pct": float(np.max(np.abs(diff[sel]))),
                                      "rms_diff_pct": float(np.sqrt(np.mean(diff[sel] ** 2))),
                                      "buildup_diff_0mm_pct": float(diff[mm["x"] == 0][0]) if np.any(mm["x"] == 0) else None}
        axs[0].plot(mm["x"] / 10, mm["y"], "o", ms=2.5, label=f"{f:g}×{f:g}")
        axs[0].plot(mm["x"] / 10, c, "-", lw=1)
        axs[1].plot(mm["x"][sel] / 10, diff[sel], "-", lw=1.2, label=f"{f:g}×{f:g}")
    axs[0].set_xlabel("profundidade (cm)"); axs[0].set_ylabel("PDD (%)"); axs[0].legend(fontsize=7, ncol=2)
    axs[0].set_title("PDD — pontos: medido; linhas: modelo")
    axs[1].axhspan(-2, 2, color="0.9"); axs[1].axhline(0, color="k", lw=.5)
    axs[1].set_xlabel("profundidade (cm)"); axs[1].set_ylabel("calculado − medido (%)"); axs[1].legend(fontsize=7)
    axs[1].set_title("Diferença além de dmax (faixa cinza ±2%)")
    fig.tight_layout(); fig.savefig(out_dir / "pdd.png", dpi=110); plt.close(fig)
    for f in sorted(scp_m):
        if f in raw:
            cval = raw[f] / raw[10.0]
        elif f in VALID_FIELDS or 2 <= f <= 40:
            y, d = calc_pdd(model, cones, f)
            cval = float(np.interp(dmax, y, d)) / raw[10.0]
        else:
            continue
        res["scp"][f"{f:g}"] = {"medido": scp_m[f], "calculado": cval, "diff_pct": 100 * (cval / scp_m[f] - 1)}
    if oar_m:
        depths = sorted(oar_m)
        calc = calc_oar_diag(model, cones, depths)
        for dep in depths:
            rm, vm = oar_m[dep][:, 0], oar_m[dep][:, 1]
            rc, vc = calc[dep]
            s = rc >= 0
            ci = np.interp(rm, rc[s], vc[s])
            inside = rm <= 170
            res["oar"][f"{dep / 10:g}cm"] = {"max_abs_diff_pct_dentro_campo": float(np.max(np.abs(ci[inside] - vm[inside])) * 100)}
    if m["perfis"]:
        res["perfis"] = {}
        fig, axs = plt.subplots(1, 2, figsize=(12, 4.4))
        cache_d = {}
        for (fs, dep, ax), meas in sorted(m["perfis"].items()):
            progress(f"Validando perfil {fs:g}×{fs:g} {ax} {dep / 10:g} cm")
            if fs not in cache_d:
                cache_d[fs] = calc_field(model, cones, fs, max(k[1] for k in m["perfis"]) + 20.0)
            pos, prof = profile_from(*cache_d[fs], dep, ax)
            err = profile_error(meas, pos, prof, fs, dep)
            key = f"{fs:g}x{fs:g} {ax} {dep / 10:g}cm"
            res["perfis"][key] = {"max_abs_diff_pct": float(np.max(np.abs(err))) if err.size else None,
                                  "penumbra_medida_mm": penumbra_80_20(meas["x"], meas["y"]),
                                  "penumbra_calculada_mm": penumbra_80_20(pos, prof)}
            a = axs[0 if ax == "crossplane" else 1]
            a.plot(meas["x"] / 10, meas["y"], "o", ms=2)
            a.plot(pos / 10, prof, "-", lw=1, label=f"{fs:g}×{fs:g} {dep / 10:g} cm")
        for a, t in zip(axs, ("crossplane (lâminas)", "inplane (diafragmas)")):
            a.set_title(f"Perfis {t} — pontos: medido"); a.set_xlabel("posição (cm)"); a.set_ylabel("dose relativa (%)")
            a.legend(fontsize=7)
        fig.tight_layout(); fig.savefig(out_dir / "perfis.png", dpi=110); plt.close(fig)
    worst_pdd = max(v["max_abs_diff_pct"] for v in res["pdd"].values())
    worst_scp = max(abs(v["diff_pct"]) for v in res["scp"].values()) if res["scp"] else None
    res["resumo"] = {"pdd_max_pct": worst_pdd, "scp_max_pct": worst_scp,
                     "aceitavel": worst_pdd <= 2.0 and (worst_scp is None or worst_scp <= 1.5)}
    (out_dir / "validacao.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    return res
