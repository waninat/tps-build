import numpy as np
import pytest

from tps.engine import ccc
from tps.engine.beam_model import BeamModelParams, FluenceGrid, aperture
from tps.engine.geometry import beam_frame
from tps.imaging.hu_ed import HUtoED
from tps.physics.data import mu_rho_water
from tps.physics.kernels import CollapsedCones
from tps.physics.spectrum import Spectrum


def _toy_cones():
    """Kernel sintético isotrópico (6 zênites × 6 azimutes) para testes rápidos."""
    dirs, A, a, B, b = [], [], [], [], []
    th = np.radians([15, 45, 75, 105, 135, 165])
    for t in th:
        for j in range(6):
            ph = 2 * np.pi * (j + 0.5) / 6
            dirs.append([np.sin(t) * np.cos(ph), np.sin(t) * np.sin(ph), np.cos(t)])
            A.append(0.9 / 36 * 5.0)
            a.append(5.0)
            B.append(0.1 / 36 * 0.3)
            b.append(0.3)
    n = len(dirs)
    return CollapsedCones(np.array(dirs), np.array(A), np.array(a), np.array(B), np.array(b),
                          np.zeros(n), np.zeros(6))


@pytest.mark.parametrize("dens", [1.0, 0.3])
def test_transport_matches_analytic_uniform_terma(dens):
    """SRS-ENG-012: TERMA uniforme → dose central = Σ_m C_m(L_m) (solução analítica)."""
    cc = _toy_cones()
    n, vox = 41, 0.5
    eta = np.full((n, n, n), dens)
    T = np.ones((n, n, n))
    w, u, v = np.array([0, 1.0, 0]), np.array([1.0, 0, 0]), np.array([0, 0, 1.0])
    d = ccc.superpose(eta, T, (vox, vox, vox), w, cc, u, v)
    half = n * vox / 2
    R = np.stack([u, v, w], axis=1)
    exp = 0.0
    for m in range(len(cc.directions)):
        g = R @ cc.directions[m]
        L = min(half / abs(x) if abs(x) > 1e-9 else 1e9 for x in g) * dens
        exp += cc.A[m] / cc.a[m] * (1 - np.exp(-cc.a[m] * L)) + cc.B[m] / cc.b[m] * (1 - np.exp(-cc.b[m] * L))
    assert d[n // 2, n // 2, n // 2] == pytest.approx(exp, rel=0.01)


def test_terma_inverse_square_and_attenuation():
    """SRS-ENG-010: TERMA no eixo segue ψ·μ/ρ·e^{-μd}·(SAD/L)² para feixe monoenergético."""
    e = np.array([2.0])
    tab, st = ccc.terma_table(e, np.array([1.0]))
    nz, ny, nx = 3, 40, 3
    eta = np.ones((nz, ny, nx))
    origin = np.array([-0.5, 0.25, -0.5])
    sp = np.array([0.5, 0.5, 0.5])
    fr = beam_frame(0, 0, 0, [0, 0, 0], 1000.0)
    fl = np.ones((11, 11))
    out = np.zeros_like(eta)
    ccc.terma_volume(eta, origin, sp, fr.source / 10, fr.w, fr.u, fr.v, 100.0, fl, -5.0, -5.0, 1.0, 1.0,
                     tab, st, out)
    y = origin[1] + np.arange(ny) * sp[1]
    mu = float(mu_rho_water(2.0))
    expected = mu * np.exp(-mu * (y - 0.0)) * (100.0 / (100.0 + y)) ** 2
    np.testing.assert_allclose(out[1, :, 1], expected, rtol=0.02)


def test_beam_frame_gantry90_source_left():
    fr = beam_frame(90, 0, 0, [0, 0, 0], 1000.0)
    np.testing.assert_allclose(fr.source, [1000.0, 0, 0], atol=1e-9)
    np.testing.assert_allclose(fr.w, [-1.0, 0, 0], atol=1e-9)


def test_aperture_area_and_transmission():
    g = FluenceGrid(-100, -100, 1.0, 1.0, 201, 201)
    p = BeamModelParams(mlc_transmission=0.005, jaw_transmission=0.001)
    lb = np.linspace(-200, 200, 81)
    ap = aperture(g, None, (-50, 50), np.full(80, -50.0), np.full(80, 50.0), lb, p)
    assert ap[100, 100] == pytest.approx(1.0)
    assert ap[100, 180] == pytest.approx(0.005, rel=1e-6)      # sob o MLC, dentro dos jaws
    assert ap[180, 100] == pytest.approx(0.001, rel=1e-6)      # MLC aberto, sob o jaw
    assert ap.sum() == pytest.approx(100 * 100, rel=0.03)


def test_hu_ed_curve_oncomed():
    c = HUtoED.from_csv("siemens_spirit_oncomed")
    assert c(np.array([-1000.0]))[0] == pytest.approx(0.0)
    assert c(np.array([240.0]))[0] == pytest.approx(1.14)
    assert c(np.array([-115.0]))[0] == pytest.approx(0.95)


def test_spectrum_mohan6_mean_energy():
    s = Spectrum.mohan(6)
    assert 1.5 < s.mean_energy < 2.2
    assert s.hardened(5.0).mean_energy > s.mean_energy


def test_beamdata_from_report_spotcheck():
    """Pontos conferidos manualmente contra o PDF (RXT 2019, S/N 154828)."""
    from pathlib import Path
    from tps.beamdata.csv_loader import load_curves
    pdd = load_curves(Path(__file__).parent / "dados" / "pdd_synergy_6MV.csv", "pdd")
    c = pdd[("elekta_synergy_agility", "6", 10.0, 10.0, 100.0)]
    assert np.interp(100.0, c["x"], c["y"]) == pytest.approx(67.7)
    assert np.interp(300.0, c["x"], c["y"]) == pytest.approx(23.5)
    c3 = pdd[("elekta_synergy_agility", "6", 3.0, 3.0, 100.0)]
    assert np.interp(0.0, c3["x"], c3["y"]) == pytest.approx(59.3)


def test_hybrid_terma_matches_exact_single_source():
    """SRS-ENG-011: TERMA híbrido (mapa do setor + traçado exato) = traçado exato quando a fonte é a mesma."""
    rng = np.random.default_rng(0)
    eta = rng.uniform(0.2, 1.5, (12, 20, 12))
    origin = np.array([-2.75, 0.25, -2.75])
    sp = np.array([0.5, 0.5, 0.5])
    fr = beam_frame(20, 0, 0, [0, 50, 0], 1000.0)
    tab, st = ccc.terma_table(np.array([2.0]), np.array([1.0]))
    fl = np.ones((21, 21))
    exact = np.zeros_like(eta)
    ccc.terma_volume(eta, origin, sp, fr.source / 10, fr.w, fr.u, fr.v, 100.0, fl, -10.0, -10.0, 1.0, 1.0, tab, st, exact)
    depth = ccc.radiological_depth(eta, origin, sp, fr.source / 10)
    hyb = np.zeros_like(eta)
    econ = np.zeros_like(eta)
    ccc.terma_hybrid(eta, origin, sp, fr.source / 10, fr.w, fr.u, fr.v, 100.0, fl, -10.0, -10.0, 1.0, 1.0, tab, st,
                     depth, 10.0, 0.0, hyb, econ)          # limiar alto: usa só o mapa do setor
    np.testing.assert_allclose(hyb, exact, rtol=1e-9)


def test_head_leakage_adds_uniform_fluence():
    from tps.engine.beam_model import energy_fluence
    g = FluenceGrid(-50, -50, 1.0, 1.0, 101, 101)
    p = BeamModelParams(head_leakage=0.001, horn=[0, 0, 0])
    psi = energy_fluence(np.zeros((101, 101)), g, p)
    assert psi.min() == pytest.approx(0.001) and psi.max() == pytest.approx(0.001)


def test_interleaf_band_raises_mean_transmission():
    g = FluenceGrid(-100, -20, 1.0, 1.0, 201, 41)
    lb = np.linspace(-200, 200, 81)
    closed = np.zeros(80)
    base = aperture(g, None, None, closed, closed, lb, BeamModelParams(mlc_transmission=0.005))
    il = aperture(g, None, None, closed, closed, lb,
                  BeamModelParams(mlc_transmission=0.005, interleaf_factor=3.0, groove_width_mm=0.7))
    # média esperada: 0,5% + (0,7/5)·(1,5% − 0,5%) = 0,64%
    assert base[:, 50:150].mean() == pytest.approx(0.005, rel=1e-6)
    assert il[:, 50:150].mean() == pytest.approx(0.0064, rel=0.08)


def test_penumbra_80_20_of_analytic_edge():
    from scipy.special import erf
    from tps.commissioning import penumbra_80_20
    x = np.arange(-100, 100.5, 0.5)
    s = 3.0
    prof = 50 * (erf((50 - x) / (np.sqrt(2) * s)) + erf((50 + x) / (np.sqrt(2) * s)))
    # para borda gaussiana, 80–20% = 2·0,8416·σ
    assert penumbra_80_20(x, prof / prof.max() * 100) == pytest.approx(2 * 0.8416 * s, rel=0.03)
