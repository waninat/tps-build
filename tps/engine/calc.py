"""Orquestração do cálculo de dose de um plano. SRS-ENG-030..033.

Cada par de pontos de controle vira um segmento (abertura média, gantry médio,
MU do segmento). Segmentos são agrupados em setores de gantry: o TERMA é somado
por setor e a superposição usa o eixo médio do setor (kernel não inclinado
dentro do setor — aproximação padrão para VMAT).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tps.dicom_io.rtplan import Beam
from tps.engine import ccc
from tps.engine.beam_model import BeamModelParams, FluenceGrid, aperture, aperture_rapida, energy_fluence
from tps.engine.geometry import beam_frame
from tps.physics.kernels import CollapsedCones
from tps.physics.spectrum import Spectrum


@dataclass
class DoseGrid:
    eta: np.ndarray          # [nz, ny, nx] densidade eletrônica relativa
    origin_mm: np.ndarray    # (x, y, z) do voxel [0,0,0]
    spacing_mm: np.ndarray   # (dx, dy, dz)


@dataclass
class Segment:
    gantry: float
    collimator: float
    couch: float
    isocenter: np.ndarray
    mu: float
    jaw_x: tuple | None
    jaw_y: tuple | None
    mlc_a: np.ndarray | None
    mlc_b: np.ndarray | None
    leaf_boundaries: np.ndarray | None
    sad: float
    # várias aberturas no mesmo gantry (entrega reconstruída do log): [(mu, jaw_x, jaw_y, mlc_a, mlc_b), ...]
    componentes: list | None = None


def _circ_mean(a_deg: float, b_deg: float) -> float:
    d = (b_deg - a_deg + 180) % 360 - 180
    return (a_deg + d / 2) % 360


def segments_from_beam(beam: Beam, isocenter_override=None) -> list[Segment]:
    segs = []
    cps = beam.control_points
    for k in range(len(cps) - 1):
        c0, c1 = cps[k], cps[k + 1]
        if c0.mu_segment <= 1e-9:
            continue
        dynamic = beam.technique in ("VMAT", "DMLC")
        if dynamic:
            mlc_a = None if c0.mlc_a is None else 0.5 * (c0.mlc_a + c1.mlc_a)
            mlc_b = None if c0.mlc_b is None else 0.5 * (c0.mlc_b + c1.mlc_b)
            jy = None if c0.jaw_y is None else tuple(0.5 * (np.array(c0.jaw_y) + np.array(c1.jaw_y)))
            jx = None if c0.jaw_x is None else tuple(0.5 * (np.array(c0.jaw_x) + np.array(c1.jaw_x)))
            g = _circ_mean(c0.gantry, c1.gantry)
        else:
            mlc_a, mlc_b, jy, jx, g = c0.mlc_a, c0.mlc_b, c0.jaw_y, c0.jaw_x, c0.gantry
        iso = np.asarray(isocenter_override if isocenter_override is not None else c0.isocenter, float)
        segs.append(Segment(g, c0.collimator, c0.couch, iso, c0.mu_segment, jx, jy, mlc_a, mlc_b,
                            beam.leaf_boundaries, beam.sad))
    return segs


def compute_dose(grid: DoseGrid, segments: list[Segment], params: BeamModelParams,
                 cones: CollapsedCones, spectrum: Spectrum | None = None,
                 sector_deg: float = 10.0, fluence_grid: FluenceGrid | None = None,
                 patient_position: str = "HFS", exact_threshold: float = 0.05) -> np.ndarray:
    """Dose (Gy) na grade, somando todos os segmentos."""
    spectrum = spectrum or Spectrum.mohan(int(params.energy_mv), params.spectrum_hardening_k)
    tab, tab_step = ccc.terma_table(spectrum.energies, spectrum.energy_fluence)
    fg = fluence_grid or FluenceGrid()
    eta = np.ascontiguousarray(grid.eta, np.float64)
    origin = np.asarray(grid.origin_mm, float) / 10.0
    spacing = np.asarray(grid.spacing_mm, float) / 10.0

    # agrupa por (setor de gantry, colimador, mesa, isocentro)
    groups: dict[tuple, list[Segment]] = {}
    for s in segments:
        key = (int(np.floor(s.gantry / sector_deg)), round(s.collimator, 1), round(s.couch, 1),
               tuple(np.round(s.isocenter, 1)))
        groups.setdefault(key, []).append(s)

    dose = np.zeros_like(eta)
    econ = np.zeros_like(eta)
    use_e = params.e_contam_amp > 0 and params.e_contam_ref_raw > 0
    e_k = params.e_contam_k_cm if use_e else 0.0
    for key, segs in groups.items():
        terma = np.zeros_like(eta)
        g_mean = np.degrees(np.angle(sum(s.mu * np.exp(1j * np.radians(s.gantry)) for s in segs))) % 360
        fr_mean = beam_frame(g_mean, segs[0].collimator, segs[0].couch, segs[0].isocenter,
                             segs[0].sad, patient_position)
        depth_sector = ccc.radiological_depth(eta, origin, spacing, fr_mean.source / 10.0)
        for s in segs:
            fr = beam_frame(s.gantry, s.collimator, s.couch, s.isocenter, s.sad, patient_position)
            if s.componentes:
                # a fluência é linear na abertura: soma ponderada pela UM antes do borramento da fonte
                ap = sum(c[0] * aperture_rapida(fg, c[1], c[2], c[3], c[4], s.leaf_boundaries, params)
                         for c in s.componentes) / max(s.mu, 1e-12)
            else:
                ap = aperture(fg, s.jaw_x, s.jaw_y, s.mlc_a, s.mlc_b, s.leaf_boundaries, params)
            psi = np.ascontiguousarray(energy_fluence(ap, fg, params) * s.mu)
            sig_e = float(getattr(params, "e_contam_sigma_mm", 0.0))
            alvo_econ = econ if not (use_e and sig_e > 0) else np.zeros_like(eta)
            ccc.terma_hybrid(eta, origin, spacing, fr.source / 10.0, fr.w, fr.u, fr.v, fr.sad / 10.0,
                             psi, fg.u0 / 10.0, fg.v0 / 10.0, fg.du / 10.0, fg.dv / 10.0, tab, tab_step,
                             depth_sector, exact_threshold * float(psi.max()), e_k, terma, alvo_econ)
            if use_e and sig_e > 0:
                # contaminação eletrônica com dependência do tamanho de campo: fluência borrada (σ no isocentro)
                from scipy.ndimage import gaussian_filter
                psi_e = np.ascontiguousarray(gaussian_filter(psi, sig_e / fg.du, mode="constant"))
                ccc.terma_hybrid(eta, origin, spacing, fr.source / 10.0, fr.w, fr.u, fr.v, fr.sad / 10.0,
                                 psi_e, fg.u0 / 10.0, fg.v0 / 10.0, fg.du / 10.0, fg.dv / 10.0, tab, tab_step,
                                 depth_sector, exact_threshold * float(psi_e.max()), e_k, np.zeros_like(eta), econ)
        dose += ccc.superpose(eta, terma, spacing, fr_mean.w, cones, fr_mean.u, fr_mean.v)
    if use_e:
        dose += params.e_contam_amp * params.e_contam_ref_raw * econ
    return dose * params.norm_dose_per_mu
