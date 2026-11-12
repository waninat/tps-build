"""Planejamento 2D (sem CT): campos retangulares sobre um fantoma virtual. SRS-2D-001..008.

Uso típico: paliativos (coluna, ossos, cérebro total, AP/PA, laterais opostos) em que o planejamento é feito com as
medidas do paciente na simulação (separação, profundidade do ponto) e não com CT.

O fantoma virtual (água ou densidade informada) pode ser:
  caixa    largura (x) × espessura AP (y) × comprimento (z)
  elipse   largura × espessura AP, extrudada em z
  contorno polígono no plano axial (x, y) — ex.: contorno de pele tirado com fio de chumbo — extrudado em z
Origem no centro do fantoma; superfície anterior em y = −espessura/2 (convenção DICOM, paciente HFS).

Técnicas: SAD (isocentro no ponto de prescrição ou informado) ou SSD (cada campo com isocentro na pele, SSD 100).
A dose é calculada pelo mesmo motor do 3D; as UM também são conferidas por tabela (TMR/Sc/Sp) quando há tabelas.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from matplotlib.path import Path as MplPath

from tps.engine.beam_model import BeamModelParams
from tps.engine.geometry import beam_frame
from tps.planejamento import calculo
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano, Prescricao
from tps.planejamento.um_tabela import TabelasFeixe, conferir_plano


@dataclass
class FantomaVirtual:
    tipo: str = "caixa"                    # caixa | elipse | contorno
    largura_mm: float = 300.0
    espessura_mm: float = 200.0            # separação AP
    comprimento_mm: float = 300.0
    contorno_xy: list | None = None        # [(x, y), ...] mm, para tipo="contorno"
    densidade: float = 1.0

    def grade(self, voxel_mm: float = 3.0, folga_mm: float = 30.0) -> calculo.GradeDose:
        if self.tipo == "contorno":
            if not self.contorno_xy or len(self.contorno_xy) < 3:
                raise ValueError("Contorno com menos de 3 pontos")
            pts = np.asarray(self.contorno_xy, float)
            x0, y0 = pts.min(axis=0) - folga_mm
            x1, y1 = pts.max(axis=0) + folga_mm
        else:
            if min(self.largura_mm, self.espessura_mm, self.comprimento_mm) <= 0:
                raise ValueError("Dimensões do fantoma devem ser positivas")
            x0, x1 = -self.largura_mm / 2 - folga_mm, self.largura_mm / 2 + folga_mm
            y0, y1 = -self.espessura_mm / 2 - folga_mm, self.espessura_mm / 2 + folga_mm
        if not 0.0 < self.densidade <= 3.0:
            raise ValueError("Densidade eletrônica relativa fora de (0, 3]")
        x = np.arange(x0, x1 + 1e-6, voxel_mm)
        y = np.arange(y0, y1 + 1e-6, voxel_mm)
        z = np.arange(-self.comprimento_mm / 2, self.comprimento_mm / 2 + 1e-6, voxel_mm)
        # volume parcial: fração de cada voxel dentro do fantoma (4×4 subamostras), para a pele cair no lugar certo
        ns = 4
        off = (np.arange(ns) + 0.5) / ns - 0.5
        sec = np.zeros((len(y), len(x)), np.float32)
        for dy in off:
            for dx in off:
                X, Y = np.meshgrid(x + dx * voxel_mm, y + dy * voxel_mm)
                if self.tipo == "caixa":
                    s = (np.abs(X) <= self.largura_mm / 2) & (np.abs(Y) <= self.espessura_mm / 2)
                elif self.tipo == "elipse":
                    s = (X / (self.largura_mm / 2)) ** 2 + (Y / (self.espessura_mm / 2)) ** 2 <= 1.0
                elif self.tipo == "contorno":
                    s = MplPath(np.asarray(self.contorno_xy)).contains_points(
                        np.column_stack([X.ravel(), Y.ravel()])).reshape(X.shape)
                else:
                    raise ValueError(f"Tipo de fantoma desconhecido: {self.tipo}")
                sec += s
        sec /= ns * ns
        eta = np.repeat(sec[None, :, :] * self.densidade, len(z), axis=0).astype(np.float32)
        return calculo.GradeDose(x, y, z, eta)


@dataclass
class CampoRetangular:
    """Campo 2D: tamanho no isocentro (X na direção das lâminas, Y na dos jaws), assimetria opcional."""
    nome: str
    gantry: float
    x1_mm: float                           # metades do campo (positivas): X1 lado −u, X2 lado +u
    x2_mm: float
    y1_mm: float
    y2_mm: float
    colimador: float = 0.0
    peso: float = 1.0
    blocos_mlc: list = field(default_factory=list)    # [(par, posição A, posição B)] ajuste manual de lâminas

    @classmethod
    def simetrico(cls, nome: str, gantry: float, x_cm: float, y_cm: float, **kw) -> "CampoRetangular":
        return cls(nome, gantry, x_cm * 5, x_cm * 5, y_cm * 5, y_cm * 5, **kw)


def campo_de_retangulo(r: CampoRetangular, colim: Colimacao, iso, energia_mv: float) -> Campo:
    jy = (-r.y1_mm, r.y2_mm)
    jx = (-r.x1_mm, r.x2_mm)
    if r.x1_mm + r.x2_mm < 10 or r.y1_mm + r.y2_mm < 10:
        raise ValueError(f"{r.nome}: campo menor que 1 cm")
    lim = colim.limites_laminas
    g = colim.gap_minimo_mm
    a, b = np.empty(colim.n_pares), np.empty(colim.n_pares)
    for i in range(colim.n_pares):
        if lim[i + 1] > jy[0] and lim[i] < jy[1]:
            a[i], b[i] = jx
        else:                                           # par sob o jaw Y: fechado no centro do campo
            c = 0.5 * (jx[0] + jx[1])
            a[i], b[i] = c - g / 2, c + g / 2
    for par, pa, pb in r.blocos_mlc:
        if not (1 <= par <= colim.n_pares) or pb - pa < g:
            raise ValueError(f"{r.nome}: ajuste manual inválido no par {par}")
        a[par - 1], b[par - 1] = pa, pb
    c = Campo(nome=r.nome, gantry=float(r.gantry), colimador=float(r.colimador), isocentro_mm=tuple(iso),
              energia_mv=energia_mv, peso=r.peso, jaw_y=jy, jaw_x=jx if colim.tem_jaw_x else None,
              mlc_a=[round(float(v), 1) for v in a], mlc_b=[round(float(v), 1) for v in b])
    if not colim.interdigitacao:
        from tps.planejamento.ajuste_mlc import verificar_mecanica
        erros = verificar_mecanica(c, colim)
        if erros:
            raise ValueError("; ".join(erros))
    return c


def entrada_na_pele(grade: calculo.GradeDose, ponto, gantry: float, colimador: float = 0.0, mesa: float = 0.0,
                    posicao: str = "HFS", passo_mm: float = 0.5) -> np.ndarray:
    """Ponto da pele onde o eixo do feixe (que passa pelo ponto) entra no fantoma."""
    fr = beam_frame(gantry, colimador, mesa, ponto, 1000.0, posicao)
    from scipy.ndimage import map_coordinates
    for t in np.arange(0.0, 1000.0, passo_mm):
        p = fr.source + t * fr.w
        idx = [[(p[2] - grade.z[0]) / (grade.z[1] - grade.z[0])], [(p[1] - grade.y[0]) / (grade.y[1] - grade.y[0])],
               [(p[0] - grade.x[0]) / (grade.x[1] - grade.x[0])]]
        if map_coordinates(grade.eta, idx, order=1, mode="constant")[0] >= 0.5 * grade.eta.max():
            return p
    raise ValueError("O feixe não entra no fantoma")


@dataclass
class Resultado2D:
    plano: Plano
    dose_fracao_gy: np.ndarray
    grade: calculo.GradeDose
    doses_por_um: dict
    ponto_prescricao: tuple
    conferencia_um: list | None


def planejar_2d(fantoma: FantomaVirtual, campos: list[CampoRetangular], prescricao: Prescricao, colim: Colimacao,
                modelo: BeamModelParams, maquina_id: str, nome_dicom_maquina: str, rotulo: str, paciente_id: str,
                paciente_nome: str = "", ponto_prescricao=(0.0, 0.0, 0.0), tecnica: str = "SAD",
                voxel_mm: float = 3.0, tabelas: TabelasFeixe | None = None, kernels=None,
                permitir_demonstracao: bool = False) -> Resultado2D:
    if tecnica not in ("SAD", "SSD"):
        raise ValueError("Técnica deve ser SAD ou SSD")
    grade = fantoma.grade(voxel_mm)
    p_rx = np.asarray(ponto_prescricao, float)
    prescricao.metodo, prescricao.ponto_mm = "ponto", tuple(float(v) for v in p_rx)
    plano = Plano(rotulo=rotulo, paciente_id=paciente_id, maquina_id=maquina_id, nome_dicom_maquina=nome_dicom_maquina,
                  prescricao=prescricao, tecnica="2D",
                  modelo_feixe={"energia_mv": modelo.energy_mv, "hash": calculo.hash_modelo(modelo),
                                "comissionado": modelo.comissionado, "demonstracao": modelo.demonstracao},
                  referencia_ct={"PatientName": paciente_nome, "PatientID": paciente_id,
                                 "fantoma_virtual": fantoma.__dict__.copy()})
    for r in campos:
        iso = p_rx if tecnica == "SAD" else entrada_na_pele(grade, p_rx, r.gantry, r.colimador)
        c = campo_de_retangulo(r, colim, np.round(iso, 1), modelo.energy_mv)
        c.ssd_mm = calculo.ssd_no_eixo(grade, c)
        plano.adicionar_campo(c)
    doses = calculo.dose_por_um(grade, plano.campos, colim, modelo, kernels, "HFS", permitir_demonstracao)
    dose_fx = calculo.normalizar(plano, doses, grade, None)
    conf = conferir_plano(plano, colim, tabelas, doses, grade, ponto_mm=p_rx) if tabelas else None
    return Resultado2D(plano, dose_fx, grade, doses, tuple(p_rx), conf)
