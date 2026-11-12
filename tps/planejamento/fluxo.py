"""Fluxo 3D conformacional de ponta a ponta (Fase 1). SRS-WF-101..106.

CT + RTSTRUCT → campos nos ângulos pedidos, conformados ao alvo → dose por UM → normalização → DVH.
A aprovação e a exportação ficam fora desta função: são atos do usuário (tps.planejamento.modelo).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from tps.dicom_io.rtstruct import Structure, dvh, rasterize, read_rtstruct
from tps.engine.beam_model import BeamModelParams
from tps.imaging.ct import read_ct_series
from tps.imaging.hu_ed import HUtoED
from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import Margem, OpcoesAjuste, ajustar, otimizar_colimador
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.otimizacao3d import field_in_field, otimizar_pesos
from tps.planejamento.um_tabela import TabelasFeixe, conferir_plano
from tps.planejamento.modelo import Campo, Plano, Prescricao
from tps.exportacao.rtplan import referencia_do_ct


@dataclass
class ResultadoPlanejamento:
    plano: Plano
    dose_fracao_gy: np.ndarray
    grade: calculo.GradeDose
    estruturas: dict[str, np.ndarray]          # máscaras na grade de dose
    dvh: dict[str, dict]
    doses_por_um: dict[str, np.ndarray]
    avisos: list = field(default_factory=list)
    fif: object = None
    conferencia_um: list | None = None
    custos_colimador: dict = field(default_factory=dict)

    @property
    def dose_total_gy(self) -> np.ndarray:
        return self.dose_fracao_gy * self.plano.prescricao.fracoes


def centroide(mascara: np.ndarray, grade: calculo.GradeDose) -> tuple:
    kz, ky, kx = np.nonzero(mascara)
    return (float(grade.x[kx].mean()), float(grade.y[ky].mean()), float(grade.z[kz].mean()))


@dataclass
class OpcoesPlano3D:
    """Opções do planejamento 3D direto (equivalentes às do Eclipse, mais as automações próprias)."""
    margem: Margem | float = 7.0                         # margem BEV (mm) ao alvo
    ajuste: OpcoesAjuste = field(default_factory=OpcoesAjuste)
    blindagens: list[tuple] = field(default_factory=list)   # [(nome da estrutura, Margem | mm)]
    prioridade_alvo: bool = True                         # blindagem não corta o alvo
    colimadores: list[float] | None = None               # ângulo por campo (None = 0)
    otimizar_colimador: bool = False
    otimizar_pesos: bool = False
    oars_pesos: list[tuple] = field(default_factory=list)   # [(nome, importância)] na otimização de pesos
    field_in_field: bool = False
    fif_niveis: tuple = (1.07, 1.05, 1.03)
    fif_um_min: float = 3.0
    estrutura_corpo: str | None = None                   # padrão: BODY/External/Corpo, se existir
    conferir_um_tabela: TabelasFeixe | None = None       # checagem independente das UM (SRS-PLN-027)


def _margem(m) -> Margem:
    return m if isinstance(m, Margem) else Margem.uniforme(float(m))


def _corpo(mascaras: dict, nome: str | None, grade) -> np.ndarray:
    if nome:
        return mascaras[nome]
    for n in mascaras:
        if n.strip().lower() in ("body", "external", "corpo", "externo", "pele", "skin", "outline"):
            return mascaras[n]
    return grade.eta > 0.3


def planejar_3d(pasta_ct: str | Path, arquivo_rs: str | Path, prescricao: Prescricao, angulos: list[float],
                colim: Colimacao, modelo: BeamModelParams, curva: HUtoED, maquina_id: str, nome_dicom_maquina: str,
                rotulo: str, margem_mm: float | None = None, voxel_mm: float = 3.0, pesos: list[float] | None = None,
                isocentro_mm: tuple | None = None, padrao_ct: str = "CT*", permitir_demonstracao: bool = False,
                kernels: str | Path | None = None, opcoes: OpcoesPlano3D | None = None) -> ResultadoPlanejamento:
    op = opcoes or OpcoesPlano3D()
    if margem_mm is not None:
        op.margem = margem_mm
    ct = read_ct_series(pasta_ct, padrao_ct)
    grade = calculo.grade_de_dose(ct, curva, voxel_mm)
    estruturas: list[Structure] = read_rtstruct(arquivo_rs)
    mascaras = {s.name: rasterize(s, grade.x, grade.y, grade.z) for s in estruturas}
    if prescricao.estrutura_alvo not in mascaras:
        raise ValueError(f"Estrutura alvo {prescricao.estrutura_alvo} não existe no RTSTRUCT")
    for nome, _ in op.blindagens:
        if nome not in mascaras:
            raise ValueError(f"Estrutura de blindagem {nome} não existe no RTSTRUCT")
    alvo = mascaras[prescricao.estrutura_alvo]
    iso = tuple(np.round(isocentro_mm if isocentro_mm is not None else centroide(alvo, grade), 1))
    eixos = (grade.x, grade.y, grade.z)

    import pydicom
    rs = pydicom.dcmread(str(arquivo_rs), stop_before_pixels=True, force=True)
    primeiro_ct = sorted(p for p in Path(pasta_ct).glob(padrao_ct) if p.is_file())[0]
    plano = Plano(rotulo=rotulo, paciente_id=str(rs.PatientID), maquina_id=maquina_id,
                  nome_dicom_maquina=nome_dicom_maquina, prescricao=prescricao,
                  posicao_paciente=ct.patient_position or "HFS", frame_of_reference=ct.frame_of_reference,
                  referencia_ct=referencia_do_ct(primeiro_ct), referencia_rtstruct=str(rs.SOPInstanceUID),
                  modelo_feixe={"energia_mv": modelo.energy_mv, "hash": calculo.hash_modelo(modelo),
                                "comissionado": modelo.comissionado, "demonstracao": modelo.demonstracao})
    alvos = [(alvo, _margem(op.margem))]
    blind = [(mascaras[n], _margem(m)) for n, m in op.blindagens]
    pesos = pesos or [1.0] * len(angulos)
    cols = op.colimadores or [0.0] * len(angulos)
    avisos, custos_col = [], {}
    for g, w, col in zip(angulos, pesos, cols):
        c = Campo(nome=f"G{int(round(g)):03d}", gantry=float(g), colimador=float(col), isocentro_mm=iso,
                  energia_mv=modelo.energy_mv, peso=float(w))
        if op.otimizar_colimador:
            c.colimador, custos_col[c.nome] = otimizar_colimador(c, colim, alvos, eixos, op=OpcoesAjuste(
                op.ajuste.ponto_encontro, op.ajuste.folga_jaw_mm, resolucao_mm=1.0), posicao=plano.posicao_paciente,
                blindagens=blind)
        r = ajustar(c, colim, alvos, blind, eixos, op.ajuste, posicao=plano.posicao_paciente,
                    prioridade_alvo=op.prioridade_alvo, nome_alvo=prescricao.estrutura_alvo)
        avisos += [f"{c.nome}: {a}" for a in r.avisos]
        c.ssd_mm = calculo.ssd_no_eixo(grade, c, posicao=plano.posicao_paciente)
        if c.ssd_mm is None:
            raise ValueError(f"{c.nome}: o eixo central não cruza o paciente")
        plano.adicionar_campo(c)

    doses = calculo.dose_por_um(grade, plano.campos, colim, modelo, kernels, plano.posicao_paciente,
                                permitir_demonstracao)
    if op.otimizar_pesos:
        w = otimizar_pesos(doses, alvo, [(mascaras[n], imp) for n, imp in op.oars_pesos])
        for c in plano.campos:
            c.peso = round(w[c.nome], 4)
    fif = None
    if op.field_in_field:
        fif = field_in_field(plano, grade, doses, alvo, _corpo(mascaras, op.estrutura_corpo, grade), colim, modelo,
                             op.fif_niveis, op.fif_um_min, kernels=kernels, posicao=plano.posicao_paciente,
                             permitir_demonstracao=permitir_demonstracao)
        avisos += fif.avisos
    dose_fx = calculo.normalizar(plano, doses, grade, alvo)
    dose_total = dose_fx * prescricao.fracoes
    hist = {n: dvh(dose_total, m, grade.voxel_cc) for n, m in mascaras.items() if m.any()}
    conf = conferir_plano(plano, colim, op.conferir_um_tabela, doses, grade) if op.conferir_um_tabela else None
    return ResultadoPlanejamento(plano, dose_fx, grade, mascaras, hist, doses, avisos, fif, conf, custos_col)
