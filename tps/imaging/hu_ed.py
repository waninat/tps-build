"""Conversão HU → densidade eletrônica relativa (água = 1). SRS-IMG-002.

A curva é por tomógrafo/protocolo e fica em machines/ct/<nome>.csv.
Interpolação linear por partes; fora da faixa, extrapola com a inclinação
do último segmento e limita a [0, densidade máxima da curva].
"""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from tps import paths


class HUtoED:
    def __init__(self, hu: list[float], ed: list[float], name: str = ""):
        order = np.argsort(hu)
        self.hu = np.asarray(hu, float)[order]
        self.ed = np.asarray(ed, float)[order]
        self.name = name
        if np.any(np.diff(self.hu) <= 0):
            raise ValueError("Curva HU→DE com HU repetido")
        if np.any(np.diff(self.ed) < 0):
            raise ValueError("Curva HU→DE não monotônica")

    @classmethod
    def from_csv(cls, path_or_name: str | Path) -> "HUtoED":
        p = Path(path_or_name)
        if not p.exists():
            p = next((d / f"{path_or_name}.csv" for d in paths.ct_dirs() if (d / f"{path_or_name}.csv").exists()),
                     paths.ct_dirs()[-1] / f"{path_or_name}.csv")
        with p.open(encoding="utf-8-sig") as fh:
            rows = [r for r in csv.DictReader(fh)]
        return cls([float(r["hu"]) for r in rows], [float(r["densidade_eletronica_rel"]) for r in rows],
                   name=p.stem)

    def __call__(self, hu: np.ndarray) -> np.ndarray:
        hu = np.asarray(hu, np.float32)
        ed = np.interp(hu, self.hu, self.ed).astype(np.float32)
        hi = hu > self.hu[-1]
        if np.any(hi):
            slope = (self.ed[-1] - self.ed[-2]) / (self.hu[-1] - self.hu[-2])
            ed[hi] = self.ed[-1] + slope * (hu[hi] - self.hu[-1])
        return np.clip(ed, 0.0, None)

    def mass_density(self, hu: np.ndarray) -> np.ndarray:
        """Densidade de massa aproximada (g/cm³) a partir da DE relativa.

        Para fótons de MV a superposição escala por densidade eletrônica; a
        densidade de massa só é usada no ray-tracing do TERMA (μ/ρ por meio).
        Aproximação: ρ ≈ DE para tecidos moles/pulmão; para osso ρ ≈ DE·1.03.
        """
        ed = self(hu)
        return np.where(ed > 1.1, ed * 1.03, ed)
