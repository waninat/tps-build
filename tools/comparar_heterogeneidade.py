"""Compara a correção de heterogeneidade com o TPS de referência em fantomas de placa (água/pulmão/osso).

    python tools/comparar_heterogeneidade.py PASTA_MONACO dados/maquinas/radioserra_precise/6MV [--agua PASTA]

Planos com "LUNG"/"PULMAO" ou "BONE"/"OSSO" no rótulo; a geometria é a de tps.casos_teste.fantomas_heterogeneidade
(pulmão 3–13 cm, ρe 0,25; osso 3–6 cm, ρe 1,6; resto água ρe 1,0 — as mesmas sobrescritas de densidade usadas no
Monaco). Compara a dose no eixo (cGy/UM) e, se houver os mesmos campos em água (--agua), o fator de correção
(heterogêneo/homogêneo), que separa o efeito da heterogeneidade das diferenças do modelo do feixe.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
from scipy.interpolate import RegularGridInterpolator  # noqa: E402

from comparar_monaco import dose_monaco, ler_pasta  # noqa: E402
from tps import paths  # noqa: E402
from tps.engine.beam_model import BeamModelParams  # noqa: E402
from tps.engine.calc import compute_dose  # noqa: E402
from tps.engine.phantom import square_field, water_box  # noqa: E402
from tps.physics.kernels import cones_do_modelo, load_mono  # noqa: E402

PLACAS = {"PULMAO": (30.0, 130.0, 0.25), "OSSO": (30.0, 60.0, 1.6), "AGUA": (0.0, 0.0, 1.0)}


def tipo(rotulo: str) -> str:
    r = rotulo.upper()
    if "LUNG" in r or "PULM" in r:
        return "PULMAO"
    if "BONE" in r or "OSSO" in r:
        return "OSSO"
    return "AGUA"


def nosso(p, cones, fs, placa, voxel, cache):
    k = (fs, placa, voxel)
    if k not in cache:
        y0, y1, rho = PLACAS[placa]
        g = water_box(min(400.0, fs * 10 * 1.5 + 160), 300.0, voxel)
        y = g.origin_mm[1] + np.arange(g.eta.shape[1]) * voxel
        sel = (y > y0) & (y < y1)
        g.eta[:, sel, :] = rho
        d = compute_dose(g, [square_field(fs, fs, mu=1.0)], p, cones) * 100.0
        nz, ny, nx = d.shape
        ax = tuple(g.origin_mm[i] + np.arange(n) * voxel for i, n in ((2, nz), (1, ny), (0, nx)))
        cache[k] = RegularGridInterpolator(ax, d, bounds_error=False, fill_value=np.nan)
    return cache[k]


def eixo_monaco(c, prof):
    dm, (zm, ym, xm) = dose_monaco(c["rd"])
    dm = dm / c["mu"] * 100.0
    it = RegularGridInterpolator((zm - c["iso"][2], ym - c["iso"][1], xm - c["iso"][0]), dm, bounds_error=False,
                                 fill_value=np.nan)
    passo = float(c["rd"].PixelSpacing[0])
    # média 3×3 no plano transversal (tira o ruído e o efeito de amostragem num ponto)
    acc = 0
    for a in (-passo, 0, passo):
        for b in (-passo, 0, passo):
            acc = acc + it(np.c_[np.full_like(prof, a), prof, np.full_like(prof, b)])
    return acc / 9, it


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pasta")
    ap.add_argument("maquina")
    ap.add_argument("--agua")
    ap.add_argument("--voxel", type=float, default=2.0)
    ap.add_argument("--png")
    a = ap.parse_args()
    p = BeamModelParams.load(Path(a.maquina) / "modelo" / "modelo_6MV.yaml")
    cones = cones_do_modelo(load_mono(paths.kernels_file()), p)
    casos = [c for c in ler_pasta(Path(a.pasta)) if tipo(c["rotulo"]) != "AGUA"]
    agua = {}
    if a.agua:
        for c in ler_pasta(Path(a.agua)):
            fs = round((c["x"][1] - c["x"][0]) / 10, 1)
            if c["alg"] == "CC" and abs(c["gantry"]) < 0.1:
                agua[fs] = c
    prof = np.arange(4.0, 280.0, 2.0)
    cache = {}
    linhas = []
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(2, 3, figsize=(16, 9))
    for c in sorted(casos, key=lambda c: (tipo(c["rotulo"]), c["x"][1])):
        fs = round((c["x"][1] - c["x"][0]) / 10, 1)
        pl = tipo(c["rotulo"])
        dm, _ = eixo_monaco(c, prof)
        itn = nosso(p, cones, fs, pl, a.voxel if fs > 4 else 1.0, cache)
        dn = itn(np.c_[np.zeros_like(prof), prof, np.zeros_like(prof)])
        y0, y1, _ = PLACAS[pl]
        zonas = {"antes": (prof >= 15) & (prof < y0 - 3), "dentro": (prof > y0 + 4) & (prof < y1 - 4),
                 "depois": (prof > y1 + 4) & (prof < y1 + 60), "longe": (prof >= y1 + 60) & (prof < 250)}
        res = {z: float(np.nanmean(dn[m] / dm[m] - 1) * 100) for z, m in zonas.items() if m.any()}
        fc = {}
        if fs in agua:
            dma, _ = eixo_monaco(agua[fs], prof)
            dna = nosso(p, cones, fs, "AGUA", a.voxel if fs > 4 else 1.0, cache)(
                np.c_[np.zeros_like(prof), prof, np.zeros_like(prof)])
            cf_m, cf_n = dm / dma, dn / dna
            fc = {z: float(np.nanmean(cf_n[m] / cf_m[m] - 1) * 100) for z, m in zonas.items() if m.any()}
            i = 0 if pl == "PULMAO" else 1
            col = {3.0: 0, 5.0: 1, 10.0: 2}.get(fs, 2)
            ax = axs[i, col]
            ax.plot(prof, cf_m, "k-", label="Monaco CC")
            ax.plot(prof, cf_n, "r--", label="TPS")
            ax.axvspan(y0, y1, color="orange" if pl == "OSSO" else "cyan", alpha=0.15)
            ax.set(title=f"{pl} {fs:g}×{fs:g}: fator de correção (het/água)", xlabel="prof (mm)")
            ax.legend()
        linhas.append((pl, fs, res, fc))
        print(f"{pl:7s} {fs:4.0f}×{fs:<3.0f} dose TPS/Monaco−1 (%): " +
              "  ".join(f"{z} {v:+5.1f}" for z, v in res.items()) +
              ("   | fator de correção TPS/Monaco−1 (%): " + "  ".join(f"{z} {v:+5.1f}" for z, v in fc.items())
               if fc else ""), flush=True)
    if a.png:
        fig.tight_layout()
        fig.savefig(a.png, dpi=80)
    return linhas


if __name__ == "__main__":
    main()
