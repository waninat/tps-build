import numpy as np
import pytest

from tps.compare.gamma import gamma_3d


def _blob(shift=0.0):
    z = y = x = np.arange(0, 40, 2.0)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    return (z, y, x), np.exp(-((X - 20 - shift) ** 2 + (Y - 20) ** 2 + (Z - 20) ** 2) / 50)


def test_identical_passes_all():
    ax, d = _blob()
    r = gamma_3d(ax, d, ax, d)
    assert r.pass_rate == 100.0 and r.mean_gamma == pytest.approx(0.0, abs=1e-9)


def test_dose_scaling_global():
    ax, d = _blob()
    assert gamma_3d(ax, d, ax, d * 1.02).pass_rate == 100.0
    r = gamma_3d(ax, d, ax, d * 1.05, dta_mm=0.5)
    assert r.pass_rate < 100.0


def test_shift_within_dta_passes():
    y = x = np.arange(0, 40, 0.25)
    Y, X = np.meshgrid(y, x, indexing="ij")
    d = np.exp(-((X - 20) ** 2 + (Y - 20) ** 2) / 50)
    ds = np.exp(-((X - 21) ** 2 + (Y - 20) ** 2) / 50)
    assert gamma_3d((y, x), d, (y, x), ds, dose_pct=1.0, dta_mm=2.0).pass_rate == 100.0
    assert gamma_3d((y, x), d, (y, x), ds, dose_pct=0.5, dta_mm=0.5).pass_rate < 90.0


def test_1d_known_value():
    x = np.arange(0, 10, 0.1)
    ref = np.ones_like(x)
    ev = np.ones_like(x) * 1.03
    r = gamma_3d((x,), ref, (x,), ev, dose_pct=3.0, dta_mm=2.0)
    assert np.nanmax(r.gamma) == pytest.approx(1.0, abs=1e-6)


# ------------------------------------------------------------------ equivalência com a implementação validada
from tps.compare.gamma import gamma_referencia


def _caso(seed, n=(14, 18, 16), passo=2.5, ruido=0.04, desloc=(0.0, 1.2, -0.8)):
    rng = np.random.default_rng(seed)
    z, y, x = (np.arange(k) * passo for k in n)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    c = np.array([z.mean(), y.mean(), x.mean()])
    ref = 2.0 * np.exp(-(((Z - c[0]) / 12) ** 2 + ((Y - c[1]) / 15) ** 2 + ((X - c[2]) / 10) ** 2))
    ev = 2.0 * np.exp(-(((Z - c[0] - desloc[0]) / 12) ** 2 + ((Y - c[1] - desloc[1]) / 15) ** 2
                        + ((X - c[2] - desloc[2]) / 10) ** 2)) * (1 + ruido * rng.standard_normal(ref.shape))
    return (z, y, x), ref, ev


@pytest.mark.parametrize("seed,local,dp,dta", [(1, False, 3, 2), (2, True, 3, 2), (3, False, 2, 2),
                                                (4, False, 1, 1), (5, True, 2, 3)])
def test_igual_a_implementacao_validada(seed, local, dp, dta):
    ax, ref, ev = _caso(seed)
    a = gamma_3d(ax, ref, ax, ev, dp, dta, 10.0, local=local)
    b = gamma_referencia(ax, ref, ax, ev, dp, dta, 10.0, local=local)
    assert a.n_evaluated == b.n_evaluated
    assert a.pass_rate == pytest.approx(b.pass_rate, abs=1e-9)
    m = np.isfinite(b.gamma)
    np.testing.assert_allclose(a.gamma[m], b.gamma[m], rtol=0, atol=1e-9)


def test_igual_com_grades_diferentes_e_eixo_decrescente():
    ax, ref, ev = _caso(7)
    z, y, x = ax
    xf = np.arange(x[0] - 5, x[-1] + 5, 1.0)                  # avaliada mais fina e maior em x
    from scipy.interpolate import RegularGridInterpolator
    f = RegularGridInterpolator(ax, ev, bounds_error=False, fill_value=None)
    Z, Y, X = np.meshgrid(z, y, xf, indexing="ij")
    evf = f(np.stack([Z, Y, X], -1))
    a = gamma_3d(ax, ref, (z, y[::-1], xf), evf[:, ::-1, :])   # y decrescente (como no DICOM às vezes)
    b = gamma_referencia(ax, ref, (z, y, xf), evf)
    assert a.pass_rate == pytest.approx(b.pass_rate, abs=1e-9)
    m = np.isfinite(b.gamma)
    np.testing.assert_allclose(a.gamma[m], b.gamma[m], atol=1e-9)
