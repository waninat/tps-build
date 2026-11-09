"""Espectro de fótons do feixe. SRS-PHY-002.

Espectro inicial: Mohan, Chui & Lidofsky, Med. Phys. 12 (1985) 592 — 6 MV.
O comissionamento ajusta um endurecimento/amolecimento paramétrico:
    w'(E) = w(E) · exp(-k · μ/ρ_W(E) · 1 g/cm²)   (k>0 endurece, k<0 amolece)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from tps.physics.data import mu_rho_water

# limite superior do bin (MeV), fluência por MeV (relativa)
MOHAN_6MV = np.array([
    (0.25, .004107824), (0.50, .1295177), (0.75, .4609022), (1.00, .4609022), (1.25, .4435880),
    (1.50, .4084962), (1.75, .4084962), (2.00, .2496504), (2.25, .2367691), (2.50, .1867142),
    (2.75, .1382681), (3.00, .1304545), (3.25, .1484442), (3.50, .09496227), (3.75, .1246583),
    (4.00, .09773248), (4.25, .08891984), (4.50, .05154921), (4.75, .04184013), (5.00, .04803306),
    (5.25, .01624749), (5.50, .02715377), (5.75, .01199324), (6.00, .01573713),
])


def _read_egs_spectrum(text: str) -> np.ndarray:
    rows = []
    for line in text.strip().splitlines()[2:]:
        a, b = line.replace(" ", "").split(",")[:2]
        rows.append((float(a), float(b)))
    return np.array(rows)


# Mohan, Chui & Lidofsky (1985): limites superiores dos bins (MeV) e fluência/MeV, como distribuídos no EGSnrc
MOHAN = {6: MOHAN_6MV}
_DIR = Path(__file__).resolve().parents[2] / "machines" / "spectra"   # de fábrica (paths.spectra_dir)
for _n in (10, 15, 24):
    _f = _DIR / f"mohan{_n}.spectrum"
    if _f.exists():
        MOHAN[_n] = _read_egs_spectrum(_f.read_text())


def _nearest_spectrum(nominal: int) -> int:
    if nominal in MOHAN:
        return nominal
    avail = sorted(MOHAN)
    return min(avail, key=lambda n: abs(n - nominal))


@dataclass
class Spectrum:
    energies: np.ndarray     # centro do bin, MeV
    fluence: np.ndarray      # fluência de fótons por bin, normalizada (soma 1)

    @classmethod
    def mohan(cls, nominal_mv: int = 6, hardening_k: float = 0.0) -> "Spectrum":
        """Espectro de Mohan do nominal mais próximo (6, 10, 15, 24 MV), ajustado pelo endurecimento k.
        Energias intermediárias (ex.: 18 MV) usam o espectro mais próximo; o comissionamento ajusta k."""
        tab = MOHAN[_nearest_spectrum(int(nominal_mv))]
        upper = tab[:, 0]
        lower = np.concatenate([[0.0], upper[:-1]])
        e = 0.5 * (upper + lower)
        w = tab[:, 1] * (upper - lower)
        return cls(e, w / w.sum()).hardened(hardening_k)

    @staticmethod
    def max_energy(nominal_mv: int) -> float:
        return float(MOHAN[_nearest_spectrum(int(nominal_mv))][-1, 0])

    def hardened(self, k: float) -> "Spectrum":
        w = self.fluence * np.exp(-k * mu_rho_water(self.energies))
        return Spectrum(self.energies.copy(), w / w.sum())

    @property
    def energy_fluence(self) -> np.ndarray:
        psi = self.fluence * self.energies
        return psi / psi.sum()

    @property
    def mean_energy(self) -> float:
        return float(np.sum(self.fluence * self.energies))
