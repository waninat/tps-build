"""Cálculo de dose do plano e normalização em UM. SRS-PLN-020..027.

Fluxo da Fase 1 (3D conformacional):
  1. Densidade eletrônica na grade de dose (CT → HU → DE, curva do tomógrafo).
  2. Dose por UM de cada campo (motor CCC herdado do SecDose; Monte Carlo na Fase 4).
  3. UM de cada campo = peso × s, com s tal que a métrica de normalização (média no alvo, isocentro ou ponto)
     receba a dose por fração prescrita (÷ isodose de prescrição).
  4. A dose é linear nas UM: a dose final é Σ UM_i · d_i, recalculada depois do arredondamento das UM.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.ndimage import map_coordinates

from tps.engine.beam_model import BeamModelParams, ModeloNaoComissionado
from tps.engine.calc import DoseGrid, Segment, compute_dose
from tps.engine.geometry import beam_frame
from tps.imaging.ct import CTVolume
from tps.imaging.hu_ed import HUtoED
from tps.physics.kernels import cones_do_modelo, load_mono
from tps.physics.spectrum import Spectrum
from tps.pipeline import resample_ct
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano

RESOLUCAO_UM = 0.1          # Elekta/Mosaiq: UM com uma casa decimal


@dataclass
class GradeDose:
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    eta: np.ndarray          # [nz, ny, nx]

    @property
    def voxel_cc(self) -> float:
        return float(abs((self.x[1] - self.x[0]) * (self.y[1] - self.y[0]) * (self.z[1] - self.z[0]))) / 1000.0

    def para_motor(self) -> DoseGrid:
        return DoseGrid(self.eta, np.array([self.x[0], self.y[0], self.z[0]]),
                        np.array([self.x[1] - self.x[0], self.y[1] - self.y[0], self.z[1] - self.z[0]]))


def grade_de_dose(ct: CTVolume, curva: HUtoED, voxel_mm: float = 3.0, caixa_mm: tuple | None = None) -> GradeDose:
    """Grade de dose cobrindo o CT (ou a caixa ((x0,x1),(y0,y1),(z0,z1)))."""
    cx, cy, cz = ct.axes()
    if caixa_mm is None:
        caixa_mm = ((cx[0], cx[-1]), (cy[0], cy[-1]), (cz[0], cz[-1]))
    x, y, z = (np.arange(a, b + 1e-6, voxel_mm) for a, b in caixa_mm)
    return GradeDose(x, y, z, resample_ct(ct, x, y, z, curva))


def hash_modelo(modelo: BeamModelParams) -> str:
    import dataclasses
    import json
    txt = json.dumps(dataclasses.asdict(modelo), sort_keys=True, default=str)
    return hashlib.sha256(txt.encode()).hexdigest()[:16]


def segmento_do_campo(c: Campo, colim: Colimacao, mu: float = 1.0, sad_mm: float = 1000.0) -> Segment:
    return Segment(gantry=c.gantry, collimator=c.colimador, couch=c.mesa,
                   isocenter=np.asarray(c.isocentro_mm, float), mu=mu,
                   jaw_x=tuple(c.jaw_x) if c.jaw_x else None, jaw_y=tuple(c.jaw_y),
                   mlc_a=np.asarray(c.mlc_a, float), mlc_b=np.asarray(c.mlc_b, float),
                   leaf_boundaries=np.asarray(colim.limites_laminas, float), sad=sad_mm)


def intervalos_arco(c: Campo) -> list[dict]:
    """Arco VMAT: um item por intervalo entre pontos de controle — gantry no meio, abertura média (as lâminas andam
    linearmente com a UM entre os pontos, como no Elekta) e peso = fração da UM do arco."""
    pc = c.pontos_controle
    out = []
    for k in range(len(pc) - 1):
        p0, p1 = pc[k], pc[k + 1]
        w = float(p1["mw"]) - float(p0["mw"])
        if w <= 1e-9:
            continue
        d = (float(p1["gantry"]) - float(p0["gantry"]) + 180.0) % 360.0 - 180.0
        med = lambda a, b: None if a is None else [0.5 * (x + y) for x, y in zip(a, b)]  # noqa: E731
        out.append({"gantry": (float(p0["gantry"]) + d / 2) % 360.0, "peso": w,
                    "mlc_a": med(p0["mlc_a"], p1["mlc_a"]), "mlc_b": med(p0["mlc_b"], p1["mlc_b"]),
                    "jaw_x": med(p0.get("jaw_x"), p1.get("jaw_x")), "jaw_y": med(p0["jaw_y"], p1["jaw_y"])})
    return out


def segmentos_do_campo(c: Campo, colim: Colimacao, sad_mm: float = 1000.0) -> list[Segment]:
    """Segmentos do campo para o motor, com mu = fração da UM do campo (campo simples: um segmento, mu = 1).
    Arco VMAT: um segmento por intervalo entre pontos de controle, no gantry do meio do intervalo."""
    if c.pontos_controle:
        return [Segment(gantry=s["gantry"], collimator=c.colimador, couch=c.mesa,
                        isocenter=np.asarray(c.isocentro_mm, float), mu=s["peso"],
                        jaw_x=tuple(s["jaw_x"]) if s.get("jaw_x") else None, jaw_y=tuple(s["jaw_y"]),
                        mlc_a=np.asarray(s["mlc_a"], float), mlc_b=np.asarray(s["mlc_b"], float),
                        leaf_boundaries=np.asarray(colim.limites_laminas, float), sad=sad_mm)
                for s in intervalos_arco(c)]
    if not c.segmentos:
        return [segmento_do_campo(c, colim, 1.0, sad_mm)]
    out = []
    for s in c.segmentos:
        out.append(Segment(gantry=c.gantry, collimator=c.colimador, couch=c.mesa,
                           isocenter=np.asarray(c.isocentro_mm, float), mu=float(s["peso"]),
                           jaw_x=tuple(s["jaw_x"]) if s.get("jaw_x") else None, jaw_y=tuple(s["jaw_y"]),
                           mlc_a=np.asarray(s["mlc_a"], float), mlc_b=np.asarray(s["mlc_b"], float),
                           leaf_boundaries=np.asarray(colim.limites_laminas, float), sad=sad_mm))
    return out


# cones reduzidos (≈ 45 direções em vez de 113) para as iterações dos otimizadores; a dose final usa os completos
BANDAS_RAPIDAS = [(0, 1, 1), (1, 6, 6), (6, 14, 8), (14, 22, 8), (22, 30, 8), (30, 42, 8), (42, 48, 6)]
_CONES: dict = {}


def _cones(kernels, modelo, rapido: bool):
    from tps import paths
    from tps.physics.kernels import DEFAULT_BANDS
    arq = str(kernels or paths.kernels_file())
    ch = (arq, modelo.energy_mv, modelo.spectrum_hardening_k, float(getattr(modelo, "escala_espalhamento", 1.0)),
          rapido)
    if ch not in _CONES:
        _CONES[ch] = cones_do_modelo(load_mono(arq), modelo, BANDAS_RAPIDAS if rapido else DEFAULT_BANDS)
    return _CONES[ch]


def dose_por_um(grade: GradeDose, campos: list[Campo], colim: Colimacao, modelo: BeamModelParams,
                kernels: str | Path | None = None, posicao: str = "HFS",
                permitir_demonstracao: bool = False, rapido: bool = False) -> dict[str, np.ndarray]:
    """Dose (Gy) por 1 UM de cada campo, na grade. rapido=True: cones reduzidos (só para iterações de otimização)."""
    if not modelo.comissionado and not (permitir_demonstracao and modelo.demonstracao):
        raise ModeloNaoComissionado("Modelo de feixe não comissionado: não calcula plano de paciente")
    cones = _cones(kernels, modelo, rapido)
    g = grade.para_motor()
    return {c.nome: compute_dose(g, segmentos_do_campo(c, colim), modelo, cones, patient_position=posicao)
            for c in campos}


def valor_em_ponto(dose: np.ndarray, grade: GradeDose, p_mm) -> float:
    ix = (p_mm[0] - grade.x[0]) / (grade.x[1] - grade.x[0])
    iy = (p_mm[1] - grade.y[0]) / (grade.y[1] - grade.y[0])
    iz = (p_mm[2] - grade.z[0]) / (grade.z[1] - grade.z[0])
    return float(map_coordinates(dose, [[iz], [iy], [ix]], order=1, mode="nearest")[0])


def metrica_normalizacao(dose: np.ndarray, plano: Plano, grade: GradeDose, mascara_alvo: np.ndarray | None) -> float:
    pr = plano.prescricao
    if pr.metodo == "media_alvo":
        if mascara_alvo is None or not mascara_alvo.any():
            raise ValueError("Normalização pela média do alvo sem máscara do alvo")
        return float(dose[mascara_alvo].mean())
    if pr.metodo == "maximo":
        corpo = grade.eta > 0.3
        return float(dose[corpo].max()) if corpo.any() else float(dose.max())
    if pr.metodo == "cobertura":
        if mascara_alvo is None or not mascara_alvo.any():
            raise ValueError("Normalização por cobertura sem máscara do alvo")
        # D_y: dose recebida por pelo menos y% do volume; a prescrição vai para x% nesse nível
        dy = float(np.percentile(dose[mascara_alvo], 100.0 - pr.cobertura_volume_pct))
        return dy * 100.0 / pr.cobertura_dose_pct
    if pr.metodo == "isocentro":
        isos = {tuple(np.round(c.isocentro_mm, 1)) for c in plano.campos}
        if len(isos) != 1:
            raise ValueError("Normalização no isocentro com mais de um isocentro")
        return valor_em_ponto(dose, grade, next(iter(isos)))
    return valor_em_ponto(dose, grade, pr.ponto_mm)


def normalizar_sem_minimo(plano, doses_um, grade, mascara_alvo) -> np.ndarray:
    return normalizar(plano, doses_um, grade, mascara_alvo, um_minimo=0.0)


def normalizar(plano: Plano, doses_um: dict[str, np.ndarray], grade: GradeDose,
               mascara_alvo: np.ndarray | None, um_minimo: float = 1.0) -> np.ndarray:
    """Define as UM dos campos e devolve a dose por fração (Gy) já com as UM arredondadas."""
    plano._exigir_rascunho()
    pesos = {c.nome: c.peso for c in plano.campos}
    if any(w <= 0 for w in pesos.values()):
        raise ValueError("Peso de campo ≤ 0")
    d_pesos = sum(pesos[n] * d for n, d in doses_um.items())
    alvo = plano.prescricao.dose_fracao_gy * 100.0 / plano.prescricao.isodose_pct
    m = metrica_normalizacao(d_pesos, plano, grade, mascara_alvo)
    if m <= 0:
        raise ValueError("Métrica de normalização nula: o alvo não recebe dose")
    s = alvo / m
    for c in plano.campos:
        c.mu = round(round(pesos[c.nome] * s / RESOLUCAO_UM) * RESOLUCAO_UM, 1)
        if c.mu < um_minimo:
            raise ValueError(f"Campo {c.nome} com menos de {um_minimo:g} UM ({c.mu:.1f}): revise os pesos")
    return sum(c.mu * doses_um[c.nome] for c in plano.campos)


def ssd_no_eixo(grade: GradeDose, campo: Campo, sad_mm: float = 1000.0, limiar_eta: float = 0.5,
                passo_mm: float = 1.0, posicao: str = "HFS") -> float | None:
    """SSD (mm) ao longo do eixo central: primeiro ponto com densidade ≥ limiar. None se não cruza o paciente."""
    fr = beam_frame(campo.gantry, campo.colimador, campo.mesa, campo.isocentro_mm, sad_mm, posicao)
    for t in np.arange(0.0, sad_mm + 400.0, passo_mm):
        p = fr.source + t * fr.w
        ix = (p[0] - grade.x[0]) / (grade.x[1] - grade.x[0])
        iy = (p[1] - grade.y[0]) / (grade.y[1] - grade.y[0])
        iz = (p[2] - grade.z[0]) / (grade.z[1] - grade.z[0])
        if not (0 <= ix <= len(grade.x) - 1 and 0 <= iy <= len(grade.y) - 1 and 0 <= iz <= len(grade.z) - 1):
            continue
        if map_coordinates(grade.eta, [[iz], [iy], [ix]], order=1)[0] >= limiar_eta:
            return float(t)
    return None
