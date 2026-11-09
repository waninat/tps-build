"""Geometria do feixe: IEC 61217 → coordenadas DICOM do paciente (HFS). SRS-ENG-001.

Convenções (paciente HFS, eixos DICOM: +x esquerda, +y posterior, +z cranial):
- Gantry 0: fonte anterior, feixe na direção +y. Gantry 90: fonte à esquerda (+x).
- No plano do isocentro: u = IEC X do colimador (direção de deslocamento das lâminas
  MLCX), v = IEC Y do colimador (direção do índice dos pares de lâminas / jaws Y).

PENDENTE (SRS-ENG-002): confirmar sentido de rotação de colimador e mesa com campos
assimétricos comparados ao TPS; implementado HFS apenas.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class BeamFrame:
    source: np.ndarray   # posição da fonte (mm, DICOM)
    w: np.ndarray        # direção do eixo central (unitária)
    u: np.ndarray        # IEC X do colimador
    v: np.ndarray        # IEC Y do colimador
    sad: float
    isocenter: np.ndarray


def _rot(axis: np.ndarray, ang_rad: float) -> np.ndarray:
    axis = axis / np.linalg.norm(axis)
    x, y, z = axis
    c, s = np.cos(ang_rad), np.sin(ang_rad)
    C = 1 - c
    return np.array([[c + x * x * C, x * y * C - z * s, x * z * C + y * s],
                     [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
                     [z * x * C - y * s, z * y * C + x * s, c + z * z * C]])


def beam_frame(gantry_deg: float, collimator_deg: float, couch_deg: float,
               isocenter_mm, sad_mm: float = 1000.0, patient_position: str = "HFS") -> BeamFrame:
    if patient_position not in ("HFS", ""):
        raise NotImplementedError(f"Posição {patient_position} ainda não suportada")
    g = np.radians(gantry_deg)
    c = np.radians(collimator_deg)
    w = np.array([-np.sin(g), np.cos(g), 0.0])
    u = np.array([np.cos(g), np.sin(g), 0.0])
    v = np.array([0.0, 0.0, 1.0])
    # colimador: gira u, v em torno do eixo do feixe
    u, v = np.cos(c) * u + np.sin(c) * v, -np.sin(c) * u + np.cos(c) * v
    # mesa: gira o referencial do feixe em torno do eixo vertical (DICOM y) no isocentro
    if couch_deg:
        R = _rot(np.array([0.0, 1.0, 0.0]), -np.radians(couch_deg))
        w, u, v = R @ w, R @ u, R @ v
    iso = np.asarray(isocenter_mm, float)
    return BeamFrame(source=iso - sad_mm * w, w=w, u=u, v=v, sad=sad_mm, isocenter=iso)
