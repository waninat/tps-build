"""Kernels de deposição de energia (EDK) e discretização em cones colapsados. SRS-PHY-003..005.

Kernels monoenergéticos gerados com EGSnrc/EDKnrc (fóton forçado a interagir na
origem, água, 48 cones de 3,75°, cascas esféricas). Arquivo .keV: para cada cone
(ic=1..48, a partir da direção do fóton) e cada casca (ix), 5 valores
(primária, 1º espalhamento, 2º, 3º+, radiativa) como fração da energia incidente.

A dose num cone colapsado m é modelada (Ahnesjö 1989) por
    C_m(r) = A_m/a_m (1 - e^{-a_m r}) + B_m/b_m (1 - e^{-b_m r})
(C = fração cumulativa de energia depositada até o raio radiológico r).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.optimize import least_squares

N_CONES_MC = 48
CONE_STEP_DEG = 3.75


def read_edk(path: str | Path, n_radii: int, n_cones: int = N_CONES_MC) -> np.ndarray:
    """Retorna array [cone, raio, componente(5)] com fração de energia por região."""
    vals = []
    for line in Path(path).read_text().split("\n"):
        tok = line.split()
        if len(tok) == 2:
            vals.append(float(tok[0]))
    a = np.asarray(vals)
    if a.size != n_cones * n_radii * 5:
        raise ValueError(f"{path}: {a.size} valores, esperado {n_cones * n_radii * 5}")
    return a.reshape(n_cones, n_radii, 5)


@dataclass
class MonoKernels:
    energies: np.ndarray       # MeV
    radii: np.ndarray          # cm, borda externa de cada casca
    edk: np.ndarray            # [E, cone, raio] fração total (soma das componentes)
    edk_primary: np.ndarray    # [E, cone, raio] só primária

    @classmethod
    def from_folder(cls, folder: str | Path, radii: np.ndarray) -> "MonoKernels":
        files = sorted(Path(folder).glob("k*.keV"))
        es, tot, pri = [], [], []
        for f in files:
            stem = f.stem
            if not stem[1:].isdigit():
                continue
            es.append(int(stem[1:]) / 1000.0)
            k = read_edk(f, len(radii))
            tot.append(k.sum(axis=2))
            pri.append(k[:, :, 0])
        order = np.argsort(es)
        return cls(np.asarray(es)[order], np.asarray(radii, float),
                   np.asarray(tot)[order], np.asarray(pri)[order])

    def at(self, e_mev: float) -> np.ndarray:
        """Kernel interpolado linearmente em energia [cone, raio]."""
        e = np.clip(e_mev, self.energies[0], self.energies[-1])
        i = int(np.clip(np.searchsorted(self.energies, e) - 1, 0, len(self.energies) - 2))
        t = (e - self.energies[i]) / (self.energies[i + 1] - self.energies[i])
        return (1 - t) * self.edk[i] + t * self.edk[i + 1]

    def polyenergetic_primary(self, energies: np.ndarray, terma_weights: np.ndarray) -> np.ndarray:
        w = np.asarray(terma_weights, float)
        w = w / w.sum()
        out = 0.0
        for ei, wi in zip(energies, w):
            e = np.clip(ei, self.energies[0], self.energies[-1])
            i = int(np.clip(np.searchsorted(self.energies, e) - 1, 0, len(self.energies) - 2))
            t = (e - self.energies[i]) / (self.energies[i + 1] - self.energies[i])
            out = out + wi * ((1 - t) * self.edk_primary[i] + t * self.edk_primary[i + 1])
        return out

    def polyenergetic(self, energies: np.ndarray, terma_weights: np.ndarray) -> np.ndarray:
        """Kernel poli-energético ponderado pela fração de TERMA de cada energia."""
        w = np.asarray(terma_weights, float)
        w = w / w.sum()
        return sum(wi * self.at(ei) for ei, wi in zip(energies, w))


# Bandas zenitais (índices de cones MC, início inclusive / fim exclusivo) e nº de azimutes
DEFAULT_BANDS = [
    (0, 1, 1), (1, 3, 6), (3, 6, 8), (6, 10, 12), (10, 14, 12), (14, 18, 12), (18, 22, 12),
    (22, 26, 12), (26, 30, 12), (30, 36, 12), (36, 42, 8), (42, 48, 6),
]


@dataclass
class CollapsedCones:
    directions: np.ndarray    # [M, 3] vetores unitários no referencial do feixe (z = direção do feixe)
    A: np.ndarray             # [M]
    a: np.ndarray             # [M] 1/cm
    B: np.ndarray
    b: np.ndarray
    zenith_deg: np.ndarray    # [M]
    fit_rms: np.ndarray       # [n_bandas] erro RMS do ajuste da fração cumulativa

    def save(self, path: str | Path, **meta) -> None:
        np.savez(path, directions=self.directions, A=self.A, a=self.a, B=self.B, b=self.b,
                 zenith_deg=self.zenith_deg, fit_rms=self.fit_rms, **meta)

    @classmethod
    def load(cls, path: str | Path) -> "CollapsedCones":
        d = np.load(path)
        return cls(d["directions"], d["A"], d["a"], d["B"], d["b"], d["zenith_deg"], d["fit_rms"])

    @property
    def total_fraction(self) -> float:
        return float(np.sum(self.A / self.a + self.B / self.b))


def _fit_band(radii: np.ndarray, cum: np.ndarray) -> tuple[float, float, float, float, float]:
    """Ajusta C(r) = A/a(1-e^{-ar}) + B/b(1-e^{-br}) aos dados cumulativos."""
    r = radii
    total = float(cum[-1])
    # Restrição: assíntota do modelo = energia total depositada na banda (até 60 cm).
    # Parâmetros livres: f (fração do termo rápido), a, b.

    def unpack(p):
        f, a, b = p
        A = f * total * a
        B = (1 - f) * total * b
        return A, a, B, b

    def model(p):
        A, a, B, b = unpack(p)
        return A / a * (1 - np.exp(-a * r)) + B / b * (1 - np.exp(-b * r))

    wts = 1.0 / np.maximum(cum, 1e-3 * total)

    def resid(p):
        return (model(p) - cum) * wts

    best = None
    for f0, a0, b0 in ((0.8, 20.0, 0.5), (0.6, 8.0, 0.3), (0.9, 40.0, 1.0), (0.5, 3.0, 0.15)):
        res = least_squares(resid, [f0, a0, b0], bounds=([0.0, 0.5, 0.02], [1.0, 500, 5.0]))
        if best is None or res.cost < best.cost:
            best = res
    A, a, B, b = unpack(best.x)
    rms = float(np.sqrt(np.mean(((model(best.x) - cum) / max(total, 1e-12)) ** 2)))
    return A, a, B, b, rms


def collapse(kernel: np.ndarray, radii: np.ndarray, bands=DEFAULT_BANDS) -> CollapsedCones:
    """Agrupa os cones MC em bandas zenitais × azimutes e ajusta exponenciais duplas."""
    dirs, As, as_, Bs, bs, zen, rms = [], [], [], [], [], [], []
    theta_edges = np.radians(np.arange(N_CONES_MC + 1) * CONE_STEP_DEG)
    for c0, c1, n_az in bands:
        band = kernel[c0:c1].sum(axis=0)          # fração por casca na banda
        cum = np.cumsum(band)
        A, a, B, b, r = _fit_band(radii, cum)
        rms.append(r)
        t0, t1 = theta_edges[c0], theta_edges[c1]
        # ângulo zenital representativo: média ponderada pelo ângulo sólido
        th = np.linspace(t0, t1, 101)
        theta = float(np.trapezoid(th * np.sin(th), th) / np.trapezoid(np.sin(th), th))
        for j in range(n_az):
            phi = 2 * np.pi * (j + 0.5) / n_az
            dirs.append([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])
            As.append(A / n_az)
            as_.append(a)
            Bs.append(B / n_az)
            bs.append(b)
            zen.append(np.degrees(theta))
    return CollapsedCones(np.asarray(dirs), np.asarray(As), np.asarray(as_), np.asarray(Bs),
                          np.asarray(bs), np.asarray(zen), np.asarray(rms))


def save_mono(mono: MonoKernels, path: str | Path, **meta) -> None:
    np.savez_compressed(path, energies=mono.energies, radii=mono.radii, edk=mono.edk,
                        edk_primary=mono.edk_primary, **meta)


def load_mono(path: str | Path) -> MonoKernels:
    d = np.load(path)
    return MonoKernels(d["energies"], d["radii"], d["edk"], d["edk_primary"])


def cones_for_spectrum(mono: MonoKernels, spectrum, bands=DEFAULT_BANDS) -> CollapsedCones:
    """Kernel poli-energético ponderado pelo TERMA na superfície (Ψ_E · μ/ρ) e colapsado."""
    from tps.physics.data import mu_rho_water
    w = spectrum.fluence * spectrum.energies * mu_rho_water(spectrum.energies)
    return collapse(mono.polyenergetic(spectrum.energies, w), mono.radii, bands)


def cones_do_modelo(mono: MonoKernels, modelo, bands=DEFAULT_BANDS) -> CollapsedCones:
    """Cones do modelo de feixe: espectro (endurecimento k) e escala do espalhamento no fantoma."""
    from tps.physics.data import mu_rho_water
    from tps.physics.spectrum import Spectrum
    sp = Spectrum.mohan(int(round(modelo.energy_mv)), modelo.spectrum_hardening_k)
    w = sp.fluence * sp.energies * mu_rho_water(sp.energies)
    tot = mono.polyenergetic(sp.energies, w)
    s = float(getattr(modelo, "escala_espalhamento", 1.0))
    if abs(s - 1.0) > 1e-9:
        prim = mono.polyenergetic_primary(sp.energies, w)
        tot = prim + s * (tot - prim)
    return collapse(tot, mono.radii, bands)
