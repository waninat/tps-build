"""Fluxo 3D conformacional de ponta a ponta (Fase 1). SRS-WF-101..106.

CT + RTSTRUCT → campos nos ângulos pedidos, conformados ao alvo → dose por UM → normalização → DVH.
A aprovação e a exportação ficam fora desta função: são atos do usuário (tps.planejamento.modelo).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tps.dicom_io.rtstruct import Structure, dvh, rasterize, read_rtstruct
from tps.engine.beam_model import BeamModelParams
from tps.imaging.ct import read_ct_series
from tps.imaging.hu_ed import HUtoED
from tps.planejamento import calculo
from tps.planejamento.conformacao import Colimacao, conformar
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

    @property
    def dose_total_gy(self) -> np.ndarray:
        return self.dose_fracao_gy * self.plano.prescricao.fracoes


def centroide(mascara: np.ndarray, grade: calculo.GradeDose) -> tuple:
    kz, ky, kx = np.nonzero(mascara)
    return (float(grade.x[kx].mean()), float(grade.y[ky].mean()), float(grade.z[kz].mean()))


def planejar_3d(pasta_ct: str | Path, arquivo_rs: str | Path, prescricao: Prescricao, angulos: list[float],
                colim: Colimacao, modelo: BeamModelParams, curva: HUtoED, maquina_id: str, nome_dicom_maquina: str,
                rotulo: str, margem_mm: float = 7.0, voxel_mm: float = 3.0, pesos: list[float] | None = None,
                isocentro_mm: tuple | None = None, padrao_ct: str = "CT*", permitir_demonstracao: bool = False,
                kernels: str | Path | None = None) -> ResultadoPlanejamento:
    ct = read_ct_series(pasta_ct, padrao_ct)
    grade = calculo.grade_de_dose(ct, curva, voxel_mm)
    estruturas: list[Structure] = read_rtstruct(arquivo_rs)
    mascaras = {s.name: rasterize(s, grade.x, grade.y, grade.z) for s in estruturas}
    if prescricao.estrutura_alvo not in mascaras:
        raise ValueError(f"Estrutura alvo {prescricao.estrutura_alvo} não existe no RTSTRUCT")
    alvo = mascaras[prescricao.estrutura_alvo]
    iso = tuple(np.round(isocentro_mm if isocentro_mm is not None else centroide(alvo, grade), 1))

    import pydicom
    rs = pydicom.dcmread(str(arquivo_rs), stop_before_pixels=True, force=True)
    primeiro_ct = sorted(p for p in Path(pasta_ct).glob(padrao_ct) if p.is_file())[0]
    plano = Plano(rotulo=rotulo, paciente_id=str(rs.PatientID), maquina_id=maquina_id,
                  nome_dicom_maquina=nome_dicom_maquina, prescricao=prescricao,
                  posicao_paciente=ct.patient_position or "HFS", frame_of_reference=ct.frame_of_reference,
                  referencia_ct=referencia_do_ct(primeiro_ct), referencia_rtstruct=str(rs.SOPInstanceUID),
                  modelo_feixe={"energia_mv": modelo.energy_mv, "hash": calculo.hash_modelo(modelo),
                                "comissionado": modelo.comissionado, "demonstracao": modelo.demonstracao})
    pesos = pesos or [1.0] * len(angulos)
    for g, w in zip(angulos, pesos):
        c = Campo(nome=f"G{int(round(g)):03d}", gantry=float(g), isocentro_mm=iso, energia_mv=modelo.energy_mv,
                  peso=float(w))
        conformar(c, alvo, (grade.x, grade.y, grade.z), colim, margem_mm, posicao=plano.posicao_paciente,
                  nome_alvo=prescricao.estrutura_alvo)
        c.ssd_mm = calculo.ssd_no_eixo(grade, c, posicao=plano.posicao_paciente)
        plano.adicionar_campo(c)

    doses = calculo.dose_por_um(grade, plano.campos, colim, modelo, kernels, plano.posicao_paciente,
                                permitir_demonstracao)
    dose_fx = calculo.normalizar(plano, doses, grade, alvo)
    dose_total = dose_fx * prescricao.fracoes
    hist = {n: dvh(dose_total, m, grade.voxel_cc) for n, m in mascaras.items() if m.any()}
    return ResultadoPlanejamento(plano, dose_fx, grade, mascaras, hist, doses)
