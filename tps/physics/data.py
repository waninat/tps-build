"""Dados físicos básicos. SRS-PHY-001.

Coeficientes de atenuação de massa da água líquida: NIST XCOM / Hubbell & Seltzer
(physics.nist.gov/PhysRefData/XrayMassCoef/ComTab/water.html).
"""
from __future__ import annotations

import numpy as np

# E (MeV), mu/rho (cm2/g), mu_en/rho (cm2/g)
_WATER = np.array([
    [0.05, 2.269e-01, 4.223e-02], [0.06, 2.059e-01, 3.190e-02], [0.08, 1.837e-01, 2.597e-02],
    [0.10, 1.707e-01, 2.546e-02], [0.15, 1.505e-01, 2.764e-02], [0.20, 1.370e-01, 2.967e-02],
    [0.30, 1.186e-01, 3.192e-02], [0.40, 1.061e-01, 3.279e-02], [0.50, 9.687e-02, 3.299e-02],
    [0.60, 8.956e-02, 3.284e-02], [0.80, 7.865e-02, 3.206e-02], [1.00, 7.072e-02, 3.103e-02],
    [1.25, 6.323e-02, 2.965e-02], [1.50, 5.754e-02, 2.833e-02], [2.00, 4.942e-02, 2.608e-02],
    [3.00, 3.969e-02, 2.281e-02], [4.00, 3.403e-02, 2.066e-02], [5.00, 3.031e-02, 1.915e-02],
    [6.00, 2.770e-02, 1.806e-02], [8.00, 2.429e-02, 1.658e-02], [10.0, 2.219e-02, 1.566e-02],
    [15.0, 1.941e-02, 1.441e-02], [20.0, 1.813e-02, 1.382e-02],
])


def _loglog(e, col):
    e = np.asarray(e, float)
    return np.exp(np.interp(np.log(e), np.log(_WATER[:, 0]), np.log(_WATER[:, col])))


def mu_rho_water(e_mev):
    """Coeficiente de atenuação de massa da água (cm²/g)."""
    return _loglog(e_mev, 1)


def mu_en_rho_water(e_mev):
    """Coeficiente de absorção de energia de massa da água (cm²/g)."""
    return _loglog(e_mev, 2)
