import numpy as np
import pytest

from tps.dicom_io.rtstruct import Structure, dvh, rasterize


def test_rasterize_square_prism_and_dvh():
    sq = np.array([[-10, -10], [10, -10], [10, 10], [-10, 10]], float)
    contours = [np.column_stack([sq, np.full(4, z)]) for z in np.arange(-9, 10, 3.0)]
    s = Structure(1, "cubo", "PTV", contours)
    x = y = np.arange(-20, 20.1, 1.0)
    z = np.arange(-9, 9.1, 1.0)
    m = rasterize(s, x, y, z)
    # 20 x 20 pontos internos por corte (centros -9.5..9.5 não existem: grade inteira → 19..21)
    assert m[:, 20, 20].all()
    assert 19 * 19 <= m[0].sum() <= 21 * 21
    dose = np.where(m, 2.0, 0.5)
    r = dvh(dose, m, 0.001)
    assert r["Dmean"] == pytest.approx(2.0)
    assert r["D95"] == pytest.approx(2.0)


# ------------------------------------------------------------------ CT mais fino que a grade de dose (até a 0.7.2 a estrutura sumia)
def _circulo(r, z, n=64):
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return np.column_stack([r * np.cos(t), r * np.sin(t), np.full(n, z)])


@pytest.mark.parametrize("ct_mm,dose_mm", [(3.0, 3.0), (2.0, 3.0), (1.5, 3.0), (1.0, 2.5), (5.0, 2.0)])
def test_estrutura_inteira_qualquer_espessura_de_corte(ct_mm, dose_mm):
    from tps.dicom_io.rtstruct import Structure, rasterize
    x = y = np.arange(-100, 101, 3.0)
    body = Structure(1, "BODY", "EXTERNAL", [_circulo(80, z) for z in np.arange(-60, 60.01, ct_mm)], (0, 255, 0))
    z = np.arange(-45, 45.01, dose_mm)
    m = rasterize(body, x, y, z)
    assert all(m[k].any() for k in range(len(z)))
    area = m[len(z) // 2].sum() * 9.0
    assert area == pytest.approx(np.pi * 80 ** 2, rel=0.05)


def test_buraco_no_mesmo_plano_e_limites_da_estrutura():
    from tps.dicom_io.rtstruct import Structure, rasterize
    x = y = np.arange(-100, 101, 3.0)
    anel = Structure(2, "ANEL", "ORGAN", [c for z in np.arange(-9, 9.01, 1.5)
                                          for c in (_circulo(50, z), _circulo(20, z))], (0, 0, 0))
    m = rasterize(anel, x, y, np.arange(-6, 6.01, 3.0))
    assert not m[2, len(y) // 2, len(x) // 2] and m[2, len(y) // 2, len(x) // 2 + 12]
    ptv = Structure(3, "PTV", "PTV", [_circulo(30, z) for z in np.arange(-6, 6.01, 1.0)], (0, 0, 0))
    z = np.arange(-30, 30.01, 3.0)
    m = rasterize(ptv, x, y, z)
    assert list(z[[k for k in range(len(z)) if m[k].any()]]) == [-6, -3, 0, 3, 6]
