"""Ajusta o borramento da fonte (σx = direção das lâminas, σy = mandíbulas) aos perfis do Monaco.

    python tools/ajustar_penumbra_monaco.py PASTA_MONACO dados/maquinas/radioserra_precise/6MV [--alg MC] [--gravar]

Os perfis do Monaco (grade de 3 mm, com ruído no MC) são comparados nos próprios pontos da grade dele: a nossa
dose é amostrada nos centros dos voxels dele com média de caixa do tamanho do voxel, e os dois lados do perfil e os
3 planos vizinhos são promediados. O erro é o RMS da dose relativa na região da penumbra (10–95% do eixo).
O σ afeta pouco a dose no eixo, exceto nos campos pequenos; depois de mudar o σ, rode o comissionamento v2
(--refinar-de) para reajustar os fatores de saída.
"""
from __future__ import annotations

import sys
from dataclasses import replace
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

PROFS = (15.0, 50.0, 100.0, 200.0)


def perfis_monaco(c):
    dm, (zm, ym, xm) = dose_monaco(c["rd"])
    iso = c["iso"]
    zc, yc, xc = zm - iso[2], ym - iso[1], xm - iso[0]
    out = {}
    iz0, ix0 = np.argmin(abs(zc)), np.argmin(abs(xc))
    for d in PROFS:
        iy = np.argmin(abs(yc - d))
        w = np.interp(d, yc[iy - 1:iy + 2], [0, 1, 0]) if 0 < iy < len(yc) - 1 else None
        # interpola linearmente em profundidade entre os 2 planos vizinhos
        j = np.searchsorted(yc, d) - 1
        f = (d - yc[j]) / (yc[j + 1] - yc[j])
        plano = (1 - f) * dm[:, j, :] + f * dm[:, j + 1, :]
        px = plano[iz0 - 1:iz0 + 2, :].mean(axis=0)          # perfil X (3 planos)
        py = plano[:, ix0 - 1:ix0 + 2].mean(axis=1)
        for eixo, pos, prof in (("X", xc, px), ("Y", zc, py)):
            meio = np.argmin(abs(pos))
            sim = np.interp(np.abs(pos[meio:]), np.abs(pos[meio::-1]), prof[meio::-1])   # espelha
            v = 0.5 * (prof[meio:] + sim)
            out[(eixo, d)] = (pos[meio:], v / v[:2].mean())
        del w
    return out


def perfis_nosso(p, cones, fsx, fsy, pontos, voxel_mm=3.0):
    vox = 1.0 if min(fsx, fsy) <= 4 else 2.0
    lado = max(160.0, max(fsx, fsy) * 10 * 1.3 + 80)
    g = water_box(lado, 230.0, vox)
    d = compute_dose(g, [square_field(fsx, fsy, ssd_mm=1000.0, mu=1.0)], p, cones)
    nz, ny, nx = d.shape
    ax = [g.origin_mm[i] + np.arange(n) * vox for i, n in ((2, nz), (1, ny), (0, nx))]
    it = RegularGridInterpolator(tuple(ax), d, bounds_error=False, fill_value=0.0)
    out = {}
    caixa = np.linspace(-voxel_mm / 2, voxel_mm / 2, 7)
    for (eixo, prof), (pos, _) in pontos.items():
        acc = 0
        for o in caixa:
            q = pos + o
            pts = np.c_[np.zeros_like(q), np.full_like(q, prof), q] if eixo == "X" else \
                np.c_[q, np.full_like(q, prof), np.zeros_like(q)]
            acc = acc + it(pts)
        v = acc / len(caixa)
        out[(eixo, prof)] = v / v[:2].mean()
    return out


def erro(mon, nos):
    e = {"X": [], "Y": []}
    for k, (pos, vm) in mon.items():
        reg = (vm > 0.10) & (vm < 0.95)
        e[k[0]].append(nos[k][reg] - vm[reg])
    return {k: float(np.sqrt(np.mean(np.concatenate(v) ** 2))) * 100 for k, v in e.items()}


def largura_80_20(pos, v):
    def cruza(n):
        i = np.where((v[:-1] >= n) & (v[1:] < n))[0][0]
        return pos[i] + (v[i] - n) / (v[i] - v[i + 1]) * (pos[i + 1] - pos[i])
    return cruza(0.2) - cruza(0.8)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pasta")
    ap.add_argument("maquina")
    ap.add_argument("--alg", default="MC")
    ap.add_argument("--sigmas", default="1.5,2.25,3.0,3.75,4.5")
    ap.add_argument("--gravar", action="store_true")
    a = ap.parse_args()
    arq = Path(a.maquina) / "modelo" / "modelo_6MV.yaml"
    p0 = BeamModelParams.load(arq)
    cones = cones_do_modelo(load_mono(paths.kernels_file()), p0)
    casos = {}
    for c in ler_pasta(Path(a.pasta)):
        fs = (c["x"][1] - c["x"][0]) / 10
        if c["alg"] == a.alg and fs <= 20.5 and abs(c["gantry"]) < 0.1:
            casos[round(fs, 1)] = c
    mon = {fs: perfis_monaco(c) for fs, c in sorted(casos.items())}
    sigmas = [float(s) for s in a.sigmas.split(",")]
    tab = {}
    for s in sigmas:
        p = replace(p0, sigma_src_x_mm=s, sigma_src_y_mm=s)
        ex, ey = [], []
        for fs, m in mon.items():
            n = perfis_nosso(p, cones, fs, fs, m)
            e = erro(m, n)
            ex.append(e["X"])
            ey.append(e["Y"])
            for eixo in "XY":
                tab[(s, fs, eixo)] = (e[eixo], largura_80_20(*m[(eixo, 100.0)]), largura_80_20(m[(eixo, 100.0)][0], n[(eixo, 100.0)]))
        print(f"σ={s:4.2f}  RMS X {np.mean(ex):5.2f}%  Y {np.mean(ey):5.2f}%   " +
              "  ".join(f"{fs:g}: X{tab[(s, fs, 'X')][0]:.1f} Y{tab[(s, fs, 'Y')][0]:.1f}" for fs in mon), flush=True)
    melhor = {}
    for eixo in "XY":
        rms = np.array([np.mean([tab[(s, fs, eixo)][0] for fs in mon]) for s in sigmas])
        i = int(np.argmin(rms))
        if 0 < i < len(sigmas) - 1:     # parábola nos 3 pontos em volta do mínimo
            x3, y3 = np.array(sigmas[i - 1:i + 2]), rms[i - 1:i + 2]
            c2, c1, _ = np.polyfit(x3, y3, 2)
            melhor[eixo] = float(-c1 / (2 * c2)) if c2 > 0 else sigmas[i]
        else:
            melhor[eixo] = sigmas[i]
    print(f"melhor σx = {melhor['X']:.2f} mm, σy = {melhor['Y']:.2f} mm")
    print("penumbra 80–20 a 10 cm (Monaco / nosso com o melhor σ mais próximo):")
    for fs in mon:
        for eixo in "XY":
            s = min(sigmas, key=lambda v: abs(v - melhor[eixo]))
            r = tab[(s, fs, eixo)]
            print(f"  {fs:g}x{fs:g} {eixo}: {r[1]:.1f} / {r[2]:.1f} mm")
    if a.gravar:
        p = replace(p0, sigma_src_x_mm=round(melhor["X"], 2), sigma_src_y_mm=round(melhor["Y"], 2))
        p.save(arq)
        print("gravado em", arq)


if __name__ == "__main__":
    main()
