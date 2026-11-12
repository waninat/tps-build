"""Fluxo completo de verificação de um plano. SRS-WF-001..004.

CT + RTPLAN (+ RTDOSE + RTSTRUCT) → dose independente → comparação.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.ndimage import map_coordinates

from tps.compare.gamma import GammaResult, gamma_3d
from tps.dicom_io.rtdose import DoseVolume
from tps.dicom_io.rtplan import Plan
from tps.engine.beam_model import BeamModelParams
from tps.engine.calc import DoseGrid, compute_dose, segments_from_beam
from tps.imaging.ct import CTVolume
from tps.imaging.hu_ed import HUtoED
from tps.physics.kernels import cones_do_modelo, load_mono
from tps.physics.spectrum import Spectrum

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class CheckResult:
    dose_gy: np.ndarray
    axes_zyx: tuple
    per_fraction: bool
    gamma: list[GammaResult] = field(default_factory=list)
    point_diffs: dict = field(default_factory=dict)
    dvh: dict = field(default_factory=dict)


def resample_ct(ct: CTVolume, x: np.ndarray, y: np.ndarray, z: np.ndarray, curve: HUtoED,
                ed_max: float = 3.0) -> np.ndarray:
    """Densidade eletrônica nos centros (x, y, z) por interpolação trilinear do HU."""
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    ix = (X - ct.origin[0]) / ct.spacing[0]
    iy = (Y - ct.origin[1]) / ct.spacing[1]
    iz = (Z - ct.origin[2]) / ct.spacing[2]
    hu = map_coordinates(ct.hu, [iz, iy, ix], order=1, mode="constant", cval=-1000.0)
    return np.clip(curve(hu), 0.0, ed_max).astype(np.float64)


def grid_axes_from_ct(ct: CTVolume, voxel_mm: float, roi_mm: tuple | None = None):
    x0, y0, z0 = ct.origin
    nz, ny, nx = ct.hu.shape
    x1, y1, z1 = x0 + (nx - 1) * ct.spacing[0], y0 + (ny - 1) * ct.spacing[1], z0 + (nz - 1) * ct.spacing[2]
    if roi_mm:
        (x0, x1), (y0, y1), (z0, z1) = roi_mm
    return (np.arange(x0, x1 + 1e-6, voxel_mm), np.arange(y0, y1 + 1e-6, voxel_mm),
            np.arange(z0, z1 + 1e-6, voxel_mm))


def compute_plan_dose(ct: CTVolume, plan: Plan, model: BeamModelParams, curve: HUtoED,
                      x: np.ndarray, y: np.ndarray, z: np.ndarray,
                      kernels_path: str | Path | None = None,
                      isocenter_override=None, sector_deg: float = 10.0) -> np.ndarray:
    """Dose por fração (Gy) na grade (z, y, x)."""
    segs = []
    for b in plan.beams:
        segs += segments_from_beam(b, isocenter_override)
    return compute_segments_dose(ct, segs, model, curve, x, y, z, kernels_path, sector_deg)


def compute_segments_dose(ct: CTVolume, segs: list, model: BeamModelParams, curve: HUtoED,
                          x: np.ndarray, y: np.ndarray, z: np.ndarray,
                          kernels_path: str | Path | None = None, sector_deg: float = 10.0,
                          eta: np.ndarray | None = None) -> np.ndarray:
    """Dose (Gy) de uma lista de segmentos já montada (plano ou entrega reconstruída do log)."""
    eta = resample_ct(ct, x, y, z, curve) if eta is None else eta
    grid = DoseGrid(eta, np.array([x[0], y[0], z[0]]),
                    np.array([x[1] - x[0], y[1] - y[0], z[1] - z[0]]))
    from tps import paths
    cones = cones_do_modelo(load_mono(kernels_path or paths.kernels_file()), model)
    return compute_dose(grid, segs, model, cones, sector_deg=sector_deg,
                        patient_position=ct.patient_position or "HFS")


def compare_with_tps(dose_calc: np.ndarray, axes_calc, tps: DoseVolume, n_fractions: int,
                     criteria=((3.0, 2.0), (2.0, 2.0)), cutoff_pct: float = 10.0) -> CheckResult:
    """Compara com o RTDOSE do TPS (soma do plano), escalando a dose calculada por fração."""
    scale = n_fractions if tps.summation.upper() == "PLAN" else 1
    calc = dose_calc * scale
    res = CheckResult(calc, axes_calc, per_fraction=(scale == 1))
    for dp, dta in criteria:
        res.gamma.append(gamma_3d(tps.axes_zyx, tps.dose_gy, axes_calc, calc, dp, dta, cutoff_pct))
    return res
