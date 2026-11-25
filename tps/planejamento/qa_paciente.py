"""Plano de verificação (QA de paciente) para IMRT: o plano do paciente recalculado num fantoma. SRS-QA-001..005.

Os campos (segmentos e UM de uma fração) são copiados para um fantoma de água virtual com o isocentro no ponto
escolhido. Dois modos, como nos sistemas comerciais:
  * "real": ângulos do plano (fantoma cilíndrico/cúbico, ex.: Octavius, ArcCHECK, Delta4);
  * "gantry0": todos os campos com gantry 0 e mesa 0 (arranjo plano no isocentro, ex.: MapCHECK, MatriXX, filme).
    Arcos VMAT: o RTPLAN é o arco real (detector preso ao gantry) e só o cálculo junta os pontos no gantry 0.
Saídas: o RTPLAN de verificação (para entregar no acelerador pelo Mosaiq), o RTDOSE 3D no fantoma (para o software
do detector comparar com a medida) e o plano coronal no isocentro (CSV, Gy) para o filme / gama no próprio TPS.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from tps.planejamento import calculo
from tps.planejamento.modelo import Plano, Prescricao
from tps.planejamento.plano_2d import FantomaVirtual


@dataclass
class ResultadoQA:
    plano: Plano
    grade: calculo.GradeDose
    dose_fx_gy: np.ndarray               # dose de UMA fração no fantoma
    iso_mm: tuple
    plano_coronal: np.ndarray            # [nz, nx] no y do isocentro
    eixos_coronal: tuple                 # (x, z)


FANTOMAS = {
    "cubo30": FantomaVirtual("caixa", 300.0, 300.0, 300.0),
    "placas30x30x20": FantomaVirtual("caixa", 300.0, 200.0, 300.0),
    "cilindro30": FantomaVirtual("elipse", 300.0, 300.0, 300.0),
}


def plano_verificacao(plano: Plano, colim, modelo, fantoma: str | FantomaVirtual = "cubo30", modo: str = "real",
                      profundidade_iso_mm: float | None = None, voxel_mm: float = 2.5, kernels=None,
                      permitir_demonstracao: bool = False, algoritmo: str = "CCC", incerteza_pct: float = 1.0,
                      progresso=None) -> ResultadoQA:
    if modo not in ("real", "gantry0"):
        raise ValueError("Modo deve ser 'real' ou 'gantry0'")
    if not plano.campos or not all(c.mu for c in plano.campos):
        raise ValueError("Calcule o plano (UM em todos os campos) antes do QA")
    fan = FANTOMAS[fantoma] if isinstance(fantoma, str) else fantoma
    g = fan.grade(voxel_mm)
    # isocentro: centro do fantoma ou a uma profundidade (a partir da superfície anterior, y = −espessura/2)
    iso = (0.0, 0.0 if profundidade_iso_mm is None else -fan.espessura_mm / 2 + float(profundidade_iso_mm), 0.0)
    qa = copy.deepcopy(plano)
    qa.rotulo = ("QA_" + plano.rotulo)[:16]
    qa.estado, qa.aprovacao, qa.historico = "RASCUNHO", {}, []
    qa.referencia_ct = {}
    qa.tecnica = "2D"                                   # fantoma virtual: sem CT (a sessão grava a técnica real)
    qa.campos_setup = []
    qa.prescricao = Prescricao(plano.prescricao.dose_fracao_gy, 1, "QA", "um_fixas")
    for c in qa.campos:
        c.isocentro_mm = iso
    calc = qa.campos
    if modo == "gantry0":
        # arco: entregue como no paciente (detector preso ao gantry); o cálculo junta tudo no gantry 0
        calc = copy.deepcopy(qa.campos)
        for c, cc in zip(qa.campos, calc):
            cc.gantry, cc.mesa = 0.0, 0.0
            for p in cc.pontos_controle or []:
                p["gantry"] = 0.0
            if not c.e_arco:
                c.gantry, c.mesa = 0.0, 0.0
    if algoritmo == "MC":                                # o mesmo algoritmo da dose aprovada do paciente
        doses, _ = calculo.dose_por_um_mc(g, calc, colim, modelo, kernels, "HFS", permitir_demonstracao,
                                          incerteza_pct=incerteza_pct, dose_na_agua=True, progresso=progresso)
    else:
        doses = calculo.dose_por_um(g, calc, colim, modelo, kernels, "HFS", permitir_demonstracao)
    d = sum(float(c.mu) * doses[c.nome] for c in qa.campos)
    jy = int(np.argmin(np.abs(g.y - iso[1])))
    return ResultadoQA(qa, g, d.astype(np.float32), iso, d[:, jy, :].astype(np.float32), (g.x, g.z))


def gravar_csv_plano(r: ResultadoQA, destino) -> None:
    """Plano coronal no isocentro (Gy, uma fração): primeira linha = x (mm), primeira coluna = z (mm)."""
    x, z = r.eixos_coronal
    with open(destino, "w", encoding="utf-8") as f:
        f.write("z\\x;" + ";".join(f"{v:.1f}" for v in x) + "\n")
        for k in range(len(z)):
            f.write(f"{z[k]:.1f};" + ";".join(f"{v:.4f}" for v in r.plano_coronal[k]) + "\n")
