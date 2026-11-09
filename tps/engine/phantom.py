"""Fantomas de água para comissionamento e testes. SRS-ENG-040."""
from __future__ import annotations

import numpy as np

from tps.engine.calc import DoseGrid, Segment


def water_box(size_xz_mm: float, depth_mm: float, voxel_mm: float, density: float = 1.0) -> DoseGrid:
    """Caixa de água com superfície em y=0 (feixe G0 entra por y=0 indo para +y)."""
    nx = int(round(size_xz_mm / voxel_mm))
    ny = int(round(depth_mm / voxel_mm))
    nz = nx
    origin = np.array([-(nx - 1) / 2 * voxel_mm, voxel_mm / 2, -(nz - 1) / 2 * voxel_mm])
    return DoseGrid(np.full((nz, ny, nx), density, np.float64), origin, np.full(3, voxel_mm))


def square_field(fs_cm_x: float, fs_cm_y: float, ssd_mm: float = 1000.0, sad_mm: float = 1000.0,
                 mu: float = 100.0, leaf_boundaries=None) -> Segment:
    """Campo retangular definido no isocentro (G0). Para SSD=SAD o isocentro fica na superfície."""
    hx, hy = fs_cm_x * 5.0, fs_cm_y * 5.0
    iso = np.array([0.0, sad_mm - ssd_mm, 0.0])
    if leaf_boundaries is None:
        leaf_boundaries = np.linspace(-200, 200, 81)
    n = len(leaf_boundaries) - 1
    return Segment(gantry=0.0, collimator=0.0, couch=0.0, isocenter=iso, mu=mu,
                   jaw_x=None, jaw_y=(-hy, hy), mlc_a=np.full(n, -hx), mlc_b=np.full(n, hx),
                   leaf_boundaries=np.asarray(leaf_boundaries, float), sad=sad_mm)


def cax_profile(dose: np.ndarray, grid: DoseGrid) -> tuple[np.ndarray, np.ndarray]:
    """Dose no eixo central (x=0, z=0) em função da profundidade (mm)."""
    nz, ny, nx = dose.shape
    y = grid.origin_mm[1] + np.arange(ny) * grid.spacing_mm[1]
    ix, iz = nx // 2, nz // 2
    if nx % 2 == 0:
        d = 0.25 * (dose[iz, :, ix] + dose[iz, :, ix - 1] + dose[iz - 1, :, ix] + dose[iz - 1, :, ix - 1])
    else:
        d = dose[iz, :, ix]
    return y, d
