"""Compara o nosso modelo de feixe com doses do Monaco (RTPLAN + RTDOSE) calculadas num fantoma de água.

    python tools/comparar_monaco.py PASTA_MONACO dados/maquinas/radioserra_precise/6MV [--saida DIR] [--voxel 2]

Para cada plano de 1 campo G0 col0 na pasta: lê aberturas, UM e isocentro do RTPLAN; lê a dose por campo
(RTDOSE BEAM, ou PLAN se não houver); calcula o mesmo campo numa caixa de água com o nosso modelo e compara
PDD, perfis (crossplane = X, inplane = Y), output (cGy/UM) e gama 3D. O tamanho do campo vem SEMPRE das
mandíbulas/lâminas do RTPLAN, nunca do nome do arquivo.

Saída: resumo.csv, pdd_<campo>.csv, perfis_<campo>.csv, e um PNG por campo.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402
import pydicom  # noqa: E402
from scipy.interpolate import RegularGridInterpolator  # noqa: E402

from tps import paths  # noqa: E402
from tps.engine.beam_model import BeamModelParams  # noqa: E402
from tps.engine.calc import compute_dose  # noqa: E402
from tps.engine.phantom import square_field, water_box  # noqa: E402
from tps.physics.kernels import cones_do_modelo, load_mono  # noqa: E402

PROFS = (15.0, 50.0, 100.0, 200.0, 300.0)


def ler_dicom(f):
    """O Monaco exporta sem preâmbulo/File Meta: deduz a sintaxe de transferência pela leitura."""
    from pydicom.dataset import FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
    d = pydicom.dcmread(f, force=True)
    if "TransferSyntaxUID" not in getattr(d, "file_meta", {}):
        d.file_meta = getattr(d, "file_meta", None) or FileMetaDataset()
        d.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian if d.is_implicit_VR else ExplicitVRLittleEndian
    return d


def ler_pasta(pasta: Path):
    planos, doses = {}, defaultdict(dict)
    for f in sorted(pasta.glob("*.dcm")):
        d = ler_dicom(f)
        if d.Modality == "RTPLAN":
            planos[d.SOPInstanceUID] = (f, d)
        elif d.Modality == "RTDOSE":
            doses[d.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID][d.DoseSummationType] = d
    casos = []
    for uid, (f, p) in planos.items():
        if len(p.BeamSequence) != 1 or uid not in doses:
            continue
        b = p.BeamSequence[0]
        cp = b.ControlPointSequence[0]
        lim = {x.RTBeamLimitingDeviceType: np.array(x.LeafJawPositions, float) for x in cp.BeamLimitingDevicePositionSequence}
        x1, x2 = lim["ASYMX"] if "ASYMX" in lim else (lim["MLCX"].min(), lim["MLCX"].max())
        if "MLCX" in lim:
            n = len(lim["MLCX"]) // 2
            a, bb = lim["MLCX"][:n], lim["MLCX"][n:]
            aberto = bb - a > 0.5
            x1, x2 = max(x1, a[aberto].min()), min(x2, bb[aberto].max())
        y1, y2 = lim["ASYMY"]
        mu = float(p.FractionGroupSequence[0].ReferencedBeamSequence[0].BeamMeterset)
        rd = doses[uid].get("BEAM") or doses[uid].get("PLAN")
        alg = "CC" if p.RTPlanLabel.upper().endswith("CC") else "MC"
        casos.append(dict(arquivo=f.name, rotulo=p.RTPlanLabel, alg=alg, x=(x1, x2), y=(y1, y2), mu=mu,
                          iso=np.array(cp.IsocenterPosition, float), ssd=float(getattr(cp, "SourceToSurfaceDistance", 1000)),
                          gantry=float(cp.GantryAngle), col=float(cp.BeamLimitingDeviceAngle), rd=rd))
    return casos


def dose_monaco(rd):
    arr = rd.pixel_array.astype(float) * float(rd.DoseGridScaling)          # [z, y, x] Gy
    x = float(rd.ImagePositionPatient[0]) + np.arange(rd.Columns) * float(rd.PixelSpacing[1])
    y = float(rd.ImagePositionPatient[1]) + np.arange(rd.Rows) * float(rd.PixelSpacing[0])
    z = float(rd.ImagePositionPatient[2]) + np.asarray(rd.GridFrameOffsetVector, float)
    return arr, (z, y, x)


def nosso(modelo, cones, fsx, fsy, voxel, cache):
    chave = (round(fsx, 1), round(fsy, 1), voxel)
    if chave not in cache:
        lado = min(410.0, max(160.0, max(fsx, fsy) * 10 * 1.25 + 80))
        g = water_box(lado, 400.0, voxel)
        d = compute_dose(g, [square_field(fsx, fsy, ssd_mm=1000.0, mu=1.0)], modelo, cones)
        nz, ny, nx = d.shape
        x = g.origin_mm[0] + np.arange(nx) * voxel
        y = g.origin_mm[1] + np.arange(ny) * voxel
        z = g.origin_mm[2] + np.arange(nz) * voxel
        cache[chave] = (d, (z, y, x))
    return cache[chave]


def penumbra(pos, perfil, lado):
    """Posição de 50% e largura 80–20% (mm) num lado do perfil (normalizado no eixo)."""
    m = pos > 0 if lado > 0 else pos < 0
    p, v = np.abs(pos[m]), perfil[m]
    o = np.argsort(p)
    p, v = p[o], v[o]

    def cruza(nivel):
        i = np.where((v[:-1] >= nivel) & (v[1:] < nivel))[0]
        if not len(i):
            return np.nan
        i = i[0]
        return p[i] + (v[i] - nivel) / (v[i] - v[i + 1]) * (p[i + 1] - p[i])
    return cruza(0.5), cruza(0.2) - cruza(0.8)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("pasta")
    ap.add_argument("maquina")
    ap.add_argument("--saida")
    ap.add_argument("--voxel", type=float, default=2.0)
    ap.add_argument("--sem-gama", action="store_true")
    a = ap.parse_args()
    pasta, maq = Path(a.pasta), Path(a.maquina)
    saida = Path(a.saida or pasta / "comparacao")
    saida.mkdir(parents=True, exist_ok=True)
    modelo = BeamModelParams.load(maq / "modelo" / "modelo_6MV.yaml")
    cones = cones_do_modelo(load_mono(paths.kernels_file()), modelo)
    casos = sorted(ler_pasta(pasta), key=lambda c: ((c.get("x")[1] - c.get("x")[0]), c["alg"]))
    cache, resumo = {}, []
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    for c in casos:
        if abs(c["gantry"]) > 0.1 or abs(c["col"]) > 0.1 or abs(c["ssd"] - 1000) > 2:
            print("pulado (só G0, col0, SSD 100):", c["arquivo"])
            continue
        fsx, fsy = (c["x"][1] - c["x"][0]) / 10, (c["y"][1] - c["y"][0]) / 10
        if abs(c["x"][0] + c["x"][1]) > 0.5 or abs(c["y"][0] + c["y"][1]) > 0.5:
            print("pulado (campo assimétrico):", c["arquivo"])
            continue
        nome = f"{fsx:g}x{fsy:g}_{c['alg']}"
        dm, (zm, ym, xm) = dose_monaco(c["rd"])
        dm = dm / c["mu"] * 100.0                                    # cGy/UM
        sup = c["iso"][1]                                            # SSD 100: isocentro na superfície
        prof_m = ym - sup
        im = RegularGridInterpolator((zm - c["iso"][2], prof_m, xm - c["iso"][0]), dm, bounds_error=False,
                                     fill_value=np.nan)
        dn, (zn, yn, xn) = nosso(modelo, cones, fsx, fsy, a.voxel if min(fsx, fsy) > 4 else min(a.voxel, 1.0), cache)
        dn = dn * 100.0
        inn = RegularGridInterpolator((zn, yn, xn), dn, bounds_error=False, fill_value=np.nan)

        # PDD
        pr = np.arange(0.0, 390.1, 1.0)
        pm = im(np.c_[np.zeros_like(pr), pr, np.zeros_like(pr)])
        pn = inn(np.c_[np.zeros_like(pr), pr, np.zeros_like(pr)])
        ok = pr >= 5
        i10 = np.argmin(abs(pr - 100))
        pdd_m, pdd_n = pm / pm[i10] * 100, pn / pn[i10] * 100
        dif_pdd = (pdd_n - pdd_m)[ok & (pr <= 350)]
        dmax_m, dmax_n = pr[ok][np.nanargmax(pm[ok])], pr[ok][np.nanargmax(pn[ok])]
        with open(saida / f"pdd_{nome}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["prof_mm", "monaco_cGy_UM", "nosso_cGy_UM", "pdd_monaco", "pdd_nosso", "dif_pct"])
            for r in zip(pr, pm, pn, pdd_m, pdd_n, pdd_n - pdd_m):
                w.writerow([f"{v:.4f}" for v in r])

        # perfis
        lim = min(220.0, fsx * 5 * 1.3 + 50)
        pos = np.arange(-lim, lim + 0.01, 1.0)
        linhas, met = [], {}
        fig, ax = plt.subplots(1, 3, figsize=(16, 4.8))
        ax[0].plot(pr, pdd_m, "k-", label="Monaco")
        ax[0].plot(pr, pdd_n, "r--", label="TPS")
        ax[0].set(title=f"PDD {nome} (norm. 10 cm)", xlabel="prof (mm)")
        ax[0].legend()
        for k, eixo in enumerate(("X", "Y")):
            for d in PROFS:
                if eixo == "X":
                    q = np.c_[np.zeros_like(pos), np.full_like(pos, d), pos]
                    meia = fsx * 5 * (1000 + d) / 1000
                else:
                    q = np.c_[pos, np.full_like(pos, d), np.zeros_like(pos)]
                    meia = fsy * 5 * (1000 + d) / 1000
                vm, vn = im(q), inn(q)
                cm, cn = vm[len(pos) // 2], vn[len(pos) // 2]
                rm, rn = vm / cm, vn / cn
                dentro = np.abs(pos) <= 0.8 * meia
                fora = (np.abs(pos) >= meia + 20) & np.isfinite(rm) & (np.abs(pos) <= 195)
                e50m = [penumbra(pos, rm, s) for s in (-1, 1)]
                e50n = [penumbra(pos, rn, s) for s in (-1, 1)]
                m_ = dict(
                    eixo_dif_pct=100 * (cn / cm - 1),
                    campo_max_dif=np.nanmax(np.abs(rn - rm)[dentro]) * 100,
                    borda50_dif_mm=np.nanmean([e50n[i][0] - e50m[i][0] for i in (0, 1)]),
                    pen_monaco_mm=np.nanmean([e50m[i][1] for i in (0, 1)]),
                    pen_nosso_mm=np.nanmean([e50n[i][1] for i in (0, 1)]),
                    fora_dif=np.nanmean((rn - rm)[fora]) * 100 if fora.any() else np.nan,
                    fora_monaco=np.nanmean(rm[fora]) * 100 if fora.any() else np.nan)
                met[(eixo, d)] = m_
                for p_, a_, b_ in zip(pos, rm, rn):
                    linhas.append([eixo, d, p_, a_ * 100, b_ * 100])
                if d in (15.0, 100.0, 300.0):
                    ax[k + 1].plot(pos, rm * 100, "k-", lw=1)
                    ax[k + 1].plot(pos, rn * 100, "r--", lw=1)
            ax[k + 1].set(title=f"Perfis {'crossplane (X)' if eixo == 'X' else 'inplane (Y)'} 1,5/10/30 cm",
                          xlabel="mm", ylim=(0, 115))
        fig.tight_layout()
        fig.savefig(saida / f"{nome}.png", dpi=90)
        plt.close(fig)
        with open(saida / f"perfis_{nome}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["eixo", "prof_mm", "pos_mm", "monaco_pct", "nosso_pct"])
            w.writerows(linhas)

        gama = np.nan
        if not a.sem_gama:
            from tps.compare.gamma import gamma_3d
            sel_z = np.abs(zm - c["iso"][2]) <= min(200, fsy * 5 * 1.4 + 40)
            sel_x = np.abs(xm - c["iso"][0]) <= min(200, fsx * 5 * 1.4 + 40)
            sel_y = (prof_m >= 3) & (prof_m <= 350)
            ref = dm[np.ix_(sel_z, sel_y, sel_x)]
            r = gamma_3d((zm[sel_z] - c["iso"][2], prof_m[sel_y], xm[sel_x] - c["iso"][0]), ref, (zn, yn, xn), dn,
                         dose_pct=2.0, dta_mm=2.0, cutoff_pct=10.0)
            gama = float(r.pass_rate) if hasattr(r, "pass_rate") else float(getattr(r, "taxa_aprovacao", np.nan))

        lin = dict(campo=f"{fsx:g}x{fsy:g}", alg=c["alg"], arquivo=c["arquivo"], rotulo=c["rotulo"],
                   cGyUM_dmax_monaco=float(np.nanmax(pm[ok])), cGyUM_dmax_nosso=float(np.nanmax(pn[ok])),
                   output_dif_pct=100 * (np.nanmax(pn[ok]) / np.nanmax(pm[ok]) - 1),
                   d10_dif_pct=100 * (pn[i10] / pm[i10] - 1),
                   dmax_monaco_mm=dmax_m, dmax_nosso_mm=dmax_n,
                   pdd_max_abs_dif=float(np.nanmax(np.abs(dif_pdd))), pdd_rms_dif=float(np.sqrt(np.nanmean(dif_pdd ** 2))),
                   gama_2_2=gama)
        for (eixo, d), m_ in met.items():
            for k_, v in m_.items():
                lin[f"{eixo}{d / 10:g}_{k_}"] = v
        resumo.append(lin)
        print(f"{nome:12s} out {lin['output_dif_pct']:+5.2f}%  d10 {lin['d10_dif_pct']:+5.2f}%  "
              f"PDD máx {lin['pdd_max_abs_dif']:4.2f}  dmax {dmax_m:.0f}/{dmax_n:.0f}  "
              f"borda X10 {met[('X', 100.0)]['borda50_dif_mm']:+.1f} mm  pen X10 {met[('X', 100.0)]['pen_monaco_mm']:.1f}/"
              f"{met[('X', 100.0)]['pen_nosso_mm']:.1f}  pen Y10 {met[('Y', 100.0)]['pen_monaco_mm']:.1f}/"
              f"{met[('Y', 100.0)]['pen_nosso_mm']:.1f}  campo X10 {met[('X', 100.0)]['campo_max_dif']:.1f}%  "
              f"fora X10 {met[('X', 100.0)]['fora_dif']:+.2f}  γ {gama:.1f}%", flush=True)

    chaves = list(dict.fromkeys(k for r in resumo for k in r))
    with open(saida / "resumo.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, chaves)
        w.writeheader()
        for r in resumo:
            w.writerow({k: (f"{v:.4f}" if isinstance(v, float) else v) for k, v in r.items()})
    print("resultado em", saida)


if __name__ == "__main__":
    main()
