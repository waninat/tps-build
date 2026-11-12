"""Exemplos com o Precise (MLCi) da Radioserra: plano 2D, plano 3D e field-in-field. Só fantomas.

    python tools/exemplo_precise.py saida/
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pydicom  # noqa: E402

from tps import fantoma, paths  # noqa: E402
from tps.engine.beam_model import BeamModelParams  # noqa: E402
from tps.exportacao.rtdose import exportar_rtdose  # noqa: E402
from tps.exportacao.rtplan import exportar_rtplan  # noqa: E402
from tps.exportacao.validacao_iod import conferir_rtplan  # noqa: E402
from tps.imaging.hu_ed import HUtoED  # noqa: E402
from tps.planejamento import calculo  # noqa: E402
from tps.planejamento.ajuste_mlc import Margem, OpcoesAjuste, ajustar, raster_da_abertura  # noqa: E402
from tps.planejamento.conformacao import Colimacao  # noqa: E402
from tps.planejamento.fluxo import OpcoesPlano3D, planejar_3d  # noqa: E402
from tps.planejamento.modelo import Campo, Plano, Prescricao  # noqa: E402
from tps.planejamento.otimizacao3d import field_in_field, indice_homogeneidade  # noqa: E402
from tps.planejamento.plano_2d import CampoRetangular, FantomaVirtual, planejar_2d  # noqa: E402
from tps.planejamento.um_tabela import TabelasFeixe  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
PASTA = RAIZ / "dados/maquinas/radioserra_precise/6MV"
NOME = "PRECISE_TESTE"


def main():
    out = Path(sys.argv[1])
    paths.set_home(out / "_home")
    paths.ensure_home()
    modelo = BeamModelParams.load(PASTA / "modelo/modelo_6MV.yaml")
    tab = TabelasFeixe.ler(PASTA, 1.0)
    colim = Colimacao.mlci()
    linhas = []

    # ------------------------------------------------------------------ 1. 2D: AP/PA de coluna, sem CT
    r2 = planejar_2d(FantomaVirtual("elipse", 360, 220, 400),
                     [CampoRetangular.simetrico("AP", 0, 8, 15), CampoRetangular.simetrico("PA", 180, 8, 15)],
                     Prescricao(20.0, 5, "ponto"), colim, modelo, "radioserra_precise", NOME, "COLUNA2D", "TESTE",
                     tabelas=tab)
    linhas.append("1) 2D — AP/PA 8×15 cm, SAD, fantoma elíptico 36×22 cm, 20 Gy/5 fx no ponto médio")
    for c, k in zip(r2.plano.campos, r2.conferencia_um):
        linhas.append(f"   {c.nome}: {c.mu:6.1f} UM  SSD {c.ssd_mm / 10:5.1f} cm  motor {k['motor_cgy_um']:.4f} × "
                      f"tabela {k['tabela_cgy_um']:.4f} cGy/UM  ({k['dif_pct']:+.2f}%)")
    r2.plano.aprovar("fisico.teste", "Físico Médico")
    exportar_rtplan(r2.plano, colim, out / "RP_2d.dcm")
    linhas.append(f"   RTPLAN 2D: {conferir_rtplan(pydicom.dcmread(out / 'RP_2d.dcm')) or 'OK'}")

    # ------------------------------------------------------------------ 2. 3D: caixa 4 campos no fantoma com CT
    caso = out / "fantoma"
    if not (caso / "RS.dcm").exists():
        fantoma.gerar_exportacao(caso, calcular_dose=False)
    for f in ("RP.dcm", "RD.dcm"):
        (caso / f).unlink(missing_ok=True)
    op = OpcoesPlano3D(margem=Margem(7, 7, 10, 10), ajuste=OpcoesAjuste("area"), otimizar_pesos=True,
                       conferir_um_tabela=tab)
    r3 = planejar_3d(caso, caso / "RS.dcm", Prescricao(50.0, 25, "PTV_fantoma"), [0, 90, 180, 270], colim, modelo,
                     HUtoED.from_csv("siemens_spirit_oncomed"), "radioserra_precise", NOME, "CAIXA3D", opcoes=op)
    linhas.append("2) 3D — caixa 4 campos, MLCi, ponto de encontro 'área', margem 7 mm (X) / 10 mm (Y), pesos otimizados")
    for c, k in zip(r3.plano.campos, r3.conferencia_um):
        linhas.append(f"   {c.nome}: {c.mu:6.1f} UM  peso {c.peso:.3f}  jaw X {c.jaw_x}  jaw Y {c.jaw_y}  "
                      f"motor × tabela {k['dif_pct']:+.2f}%")
    h = r3.dvh["PTV_fantoma"]
    linhas.append(f"   PTV: D98 {h['D98']:.2f}  D95 {h['D95']:.2f}  D2 {h['D2']:.2f} Gy  HI "
                  f"{indice_homogeneidade(r3.dose_total_gy, r3.estruturas['PTV_fantoma']):.3f}")
    r3.plano.aprovar("fisico.teste", "Físico Médico")
    rp = exportar_rtplan(r3.plano, colim, out / "RP_3d.dcm")
    exportar_rtdose(r3.dose_total_gy, r3.grade.x, r3.grade.y, r3.grade.z, rp, out / "RD_3d.dcm")
    linhas.append(f"   RTPLAN 3D: {conferir_rtplan(pydicom.dcmread(out / 'RP_3d.dcm')) or 'OK'}")

    # ------------------------------------------------------------------ 3. field-in-field sem cunha
    h_ = 4.0
    x, y, z = np.arange(-150, 151, h_), np.arange(-160, 161, h_), np.arange(-120, 121, h_)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    sep = 120 + (Z + 120) / 240 * 140
    corpo = (np.abs(Y) <= sep / 2) & (np.abs(X) <= 140)
    g = calculo.GradeDose(x, y, z, corpo.astype(np.float32))
    ptv = (np.abs(X) <= 50) & (np.abs(Y) <= 30) & (np.abs(Z) <= 90)
    p = Plano("APPA_FIF", "TESTE", "radioserra_precise", NOME, Prescricao(40.0, 20, "PTV"))
    for gnt in (0, 180):
        c = Campo(f"G{gnt:03d}", float(gnt))
        ajustar(c, colim, [(ptv, Margem.uniforme(7))], eixos=(x, y, z))
        p.adicionar_campo(c)
    doses = calculo.dose_por_um(g, p.campos, colim, modelo)
    d0 = calculo.normalizar(p, doses, g, ptv)
    fif = field_in_field(p, g, doses, ptv, corpo, colim, modelo)
    d1 = calculo.normalizar(p, doses, g, ptv)
    linhas.append("3) Field-in-field automático — AP/PA, separação de 12 a 26 cm ao longo do campo, sem cunha")
    linhas.append(f"   HI do PTV {indice_homogeneidade(d0, ptv):.3f} → {indice_homogeneidade(d1, ptv):.3f};  "
                  f"D98 {np.percentile(d0[ptv], 2) * 20:.1f} → {np.percentile(d1[ptv], 2) * 20:.1f} Gy;  "
                  f"volume > 105% {((d0 > 2.1) & corpo).sum() * g.voxel_cc:.0f} → {((d1 > 2.1) & corpo).sum() * g.voxel_cc:.0f} cc")
    linhas.append("   campos: " + ", ".join(f"{c.nome} {c.mu:.1f} UM" for c in p.campos))
    texto = "\n".join(linhas)
    print(texto)
    (out / "resumo.txt").write_text(texto, encoding="utf-8")

    # ------------------------------------------------------------------ figura
    fig, ax = plt.subplots(1, 3, figsize=(16, 5.2))
    gg = r3.grade
    k = int(np.argmin(np.abs(gg.z - r3.plano.campos[0].isocentro_mm[2])))
    ax[0].imshow(gg.eta[k], cmap="gray", extent=[gg.x[0], gg.x[-1], gg.y[-1], gg.y[0]], vmin=0, vmax=1.6)
    cs = ax[0].contour(gg.x, gg.y, r3.dose_total_gy[k] / 50, levels=[0.5, 0.8, 0.95, 1.05],
                       colors=["#2ca02c", "#ffbf00", "#ff7f0e", "#d62728"], linewidths=1.2)
    ax[0].clabel(cs, fmt=lambda v: f"{v * 100:.0f}%", fontsize=7)
    ax[0].contour(gg.x, gg.y, r3.estruturas["PTV_fantoma"][k], levels=[0.5], colors="m", linestyles="--")
    ax[0].set_title("3D Precise: caixa 4 campos (isodoses)", fontsize=10)
    c = r3.plano.campos[1]
    lim = colim.limites_laminas
    for i in range(colim.n_pares):
        if lim[i + 1] < -70 or lim[i] > 70:
            continue
        ax[1].add_patch(plt.Rectangle((-120, lim[i]), c.mlc_a[i] + 120, 10, color="#5b6b7a", ec="w", lw=.4))
        ax[1].add_patch(plt.Rectangle((c.mlc_b[i], lim[i]), 120 - c.mlc_b[i], 10, color="#5b6b7a", ec="w", lw=.4))
    for v_ in c.jaw_y:
        ax[1].axhline(v_, color="orange")
    for u_ in c.jaw_x:
        ax[1].axvline(u_, color="tab:blue", ls=":")
    from tps.planejamento.ajuste_mlc import bev_da_estrutura, grade_bev
    u, v = grade_bev(80, 0.5)
    b = bev_da_estrutura(r3.estruturas["PTV_fantoma"], (gg.x, gg.y, gg.z), c, u, v)
    ax[1].contour(u, v, b.m, levels=[0.5], colors="m")
    ax[1].set_xlim(-70, 70)
    ax[1].set_ylim(-70, 70)
    ax[1].set_aspect("equal")
    ax[1].set_title(f"BEV {c.nome}: MLCi 1 cm, ponto 'área' (jaw X azul, Y laranja)", fontsize=10)
    iz = np.arange(len(z))
    perf0 = [d0[i][ptv[i]].mean() / 2 * 100 if ptv[i].any() else np.nan for i in iz]
    perf1 = [d1[i][ptv[i]].mean() / 2 * 100 if ptv[i].any() else np.nan for i in iz]
    ax[2].plot(z / 10, perf0, label="AP/PA aberto")
    ax[2].plot(z / 10, perf1, label="field-in-field automático")
    ax[2].axhline(100, color="gray", lw=.6)
    ax[2].set_xlabel("posição ao longo do campo (cm) — separação 12 → 26 cm")
    ax[2].set_ylabel("dose média no PTV (% da prescrição)")
    ax[2].legend(fontsize=8)
    ax[2].set_title("Sem cunha: field-in-field compensa a separação", fontsize=10)
    fig.tight_layout()
    fig.savefig(out / "exemplos_precise.png", dpi=130)


if __name__ == "__main__":
    main()
