"""Exemplo da Fase 1 de ponta a ponta, em fantoma sintético (sem dado de paciente).

    python tools/exemplo_planejamento_3d.py saida/ [--modelo dados/maquinas/.../modelo_6MV.yaml]

Gera o fantoma (CT + RTSTRUCT), planeja uma caixa de 4 campos conformada ao PTV, normaliza (média do PTV = dose/fx),
aprova, exporta RTPLAN + RTDOSE, relê o RTPLAN, recalcula e compara, e desenha isodoses e BEV.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pydicom  # noqa: E402

from tps import fantoma, paths  # noqa: E402
from tps.dicom_io.rtplan import read_rtplan  # noqa: E402
from tps.engine.beam_model import BeamModelParams  # noqa: E402
from tps.exportacao.rtdose import exportar_rtdose  # noqa: E402
from tps.exportacao.rtplan import exportar_rtplan  # noqa: E402
from tps.exportacao.validacao_iod import conferir_rtplan  # noqa: E402
from tps.imaging.ct import read_ct_series  # noqa: E402
from tps.imaging.hu_ed import HUtoED  # noqa: E402
from tps.pipeline import compute_plan_dose  # noqa: E402
from tps.planejamento.conformacao import Colimacao  # noqa: E402
from tps.planejamento.fluxo import planejar_3d  # noqa: E402
from tps.planejamento.modelo import Prescricao  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
MODELO = RAIZ / "dados/maquinas/oncomed_synergy_154828/6MV/modelo/modelo_6MV.yaml"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("saida")
    ap.add_argument("--modelo", default=str(MODELO))
    a = ap.parse_args()
    out = Path(a.saida)
    paths.set_home(out / "_home")
    paths.ensure_home()
    caso = out / "fantoma"
    fantoma.gerar_exportacao(caso, calcular_dose=False)
    for f in ("RP.dcm", "RD.dcm"):           # o plano/dose "do TPS" do fantoma não interessam aqui
        (caso / f).unlink(missing_ok=True)

    modelo = BeamModelParams.load(a.modelo)
    curva = HUtoED.from_csv("siemens_spirit_oncomed")
    colim = Colimacao.agility()
    rx = Prescricao(50.0, 25, "PTV_fantoma")
    r = planejar_3d(caso, caso / "RS.dcm", rx, [0, 90, 180, 270], colim, modelo, curva, "oncomed_synergy_154828",
                    "SYNERGY_TESTE", "CAIXA4_TESTE", margem_mm=7.0, voxel_mm=3.0)
    p = r.plano
    p.aprovar("fisico.teste", "Físico Médico")
    rp = exportar_rtplan(p, colim, out / "RP_tps.dcm")
    exportar_rtdose(r.dose_total_gy, r.grade.x, r.grade.y, r.grade.z, rp, out / "RD_tps.dcm")
    problemas = conferir_rtplan(pydicom.dcmread(out / "RP_tps.dcm"))

    lido = read_rtplan(out / "RP_tps.dcm")
    d2 = compute_plan_dose(read_ct_series(caso, "CT*"), lido, modelo, curva, r.grade.x, r.grade.y, r.grade.z) * rx.fracoes
    dif = float(np.max(np.abs(d2 - r.dose_total_gy)) / r.dose_total_gy.max() * 100)

    print(f"Plano {p.rotulo}: {rx.dose_total_gy:g} Gy / {rx.fracoes} fx em {rx.estrutura_alvo}")
    for c in p.campos:
        print(f"  {c.nome}: gantry {c.gantry:5.1f}°  UM {c.mu:6.1f}  SSD {c.ssd_mm / 10:5.1f} cm  "
              f"jaw Y {c.jaw_y[0]:.1f}/{c.jaw_y[1]:.1f} mm")
    for n, h in r.dvh.items():
        print(f"  {n:14s} {h['volume_cc']:7.1f} cc  Dmean {h['Dmean']:6.2f}  D95 {h['D95']:6.2f}  "
              f"D2 {h['D2']:6.2f}  Dmax {h['Dmax']:6.2f} Gy")
    print(f"  conferência do RTPLAN: {'OK' if not problemas else problemas}")
    print(f"  RTPLAN relido e recalculado: diferença máxima {dif:.4f}% da dose máxima")
    print(f"  hash da aprovação: {p.aprovacao['hash'][:16]}…")

    # ------------------------------------------------------------------ figura
    g = r.grade
    k = int(np.argmin(np.abs(g.z - p.campos[0].isocentro_mm[2])))
    fig, ax = plt.subplots(1, 2, figsize=(11, 5))
    ext = [g.x[0], g.x[-1], g.y[-1], g.y[0]]
    ax[0].imshow(g.eta[k], cmap="gray", extent=ext, vmin=0, vmax=1.6)
    niveis = [0.3, 0.5, 0.8, 0.95, 1.05]
    cores = ["#1f77b4", "#2ca02c", "#ffbf00", "#ff7f0e", "#d62728"]
    cs = ax[0].contour(g.x, g.y, r.dose_total_gy[k] / rx.dose_total_gy, levels=niveis, colors=cores, linewidths=1.2)
    ax[0].clabel(cs, fmt=lambda v: f"{v * 100:.0f}%", fontsize=7)
    ax[0].contour(g.x, g.y, r.estruturas["PTV_fantoma"][k], levels=[0.5], colors="m", linewidths=1.5,
                  linestyles="--")
    ax[0].set_title(f"Axial no isocentro: isodoses (% de {rx.dose_total_gy:g} Gy), PTV tracejado", fontsize=10)
    ax[0].set_xlabel("x (mm)")
    ax[0].set_ylabel("y (mm)")
    c = p.campos[0]
    lim = colim.limites_laminas
    for i in range(colim.n_pares):
        if lim[i + 1] < -60 or lim[i] > 60:
            continue
        ax[1].add_patch(plt.Rectangle((-100, lim[i]), c.mlc_a[i] + 100, lim[i + 1] - lim[i], color="#5b6b7a", ec="w", lw=.3))
        ax[1].add_patch(plt.Rectangle((c.mlc_b[i], lim[i]), 100 - c.mlc_b[i], lim[i + 1] - lim[i], color="#5b6b7a", ec="w", lw=.3))
    ax[1].axhline(c.jaw_y[0], color="orange")
    ax[1].axhline(c.jaw_y[1], color="orange")
    from tps.planejamento.conformacao import pontos_do_alvo, projetar_bev
    u, v = projetar_bev(pontos_do_alvo(r.estruturas["PTV_fantoma"], g.x, g.y, g.z), c)
    ax[1].plot(u, v, ",", color="m")
    ax[1].set_xlim(-60, 60)
    ax[1].set_ylim(-60, 60)
    ax[1].set_aspect("equal")
    ax[1].set_title(f"BEV {c.nome}: lâminas (cinza), jaws Y (laranja), PTV (magenta)", fontsize=10)
    ax[1].set_xlabel("u — direção das lâminas (mm)")
    ax[1].set_ylabel("v (mm)")
    fig.tight_layout()
    fig.savefig(out / "plano_exemplo.png", dpi=130)


if __name__ == "__main__":
    main()
