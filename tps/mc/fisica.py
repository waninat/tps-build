"""Física do Monte Carlo de fótons de megavoltagem e elétrons secundários. SRS-MC-001.

Materiais no estilo XVMC/VMC++ (Fippel 1999; Kawrakow, Fippel & Friedrich 1996): todo voxel é água com a
densidade eletrônica relativa η do voxel, mais fatores por material para o que não escala com elétrons —
produção de pares (∝ Z por elétron), poder de freamento (potencial médio de excitação), radiativo e espalhamento
múltiplo (comprimento de radiação). O material sai de η (ar, pulmão, tecido mole, osso, com transição suave).

Dados da água:
  * atenuação total: NIST XCOM (tps.physics.data);
  * Compton: Klein–Nishina por elétron (sem ligação: desprezível acima de ~100 keV);
  * pares: XCOM total − Compton (E > 1,022 MeV); abaixo de ~100 keV o resto (fotoelétrico + Rayleigh) é absorção local;
  * colisão (restrito, Møller): fórmula de Bethe da ICRU 37 com o efeito de densidade de Sternheimer
    (água: I = 75 eV, C̄ = 3,5017, x0 = 0,2400, x1 = 2,8004, a = 0,09116, m = 3,4773);
  * radiativo: ESTAR (Berger & Seltzer), interpolado log-log;
  * espalhamento múltiplo: Highland (PDG) com X0 da água = 36,08 g/cm².
Unidades: MeV, cm, g/cm³.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tps.physics.data import _WATER

ME = 0.51099895            # MeV
RE = 2.8179403262e-13      # cm
NA = 6.02214076e23
ZA_AGUA = 0.55508          # Z/A da água (mol/g)
NE_AGUA = ZA_AGUA * NA     # elétrons por grama
X0_AGUA = 36.08            # g/cm²
I_AGUA = 75.0e-6           # MeV

# ESTAR, água líquida: E (MeV), poder de freamento radiativo (MeV cm²/g)
_RAD_AGUA = np.array([
    [0.01, 3.898e-3], [0.02, 3.927e-3], [0.05, 3.990e-3], [0.1, 4.130e-3], [0.2, 4.513e-3],
    [0.5, 6.073e-3], [1.0, 1.293e-2], [2.0, 2.614e-2], [3.0, 4.054e-2], [5.0, 7.164e-2],
    [7.0, 1.044e-1], [10.0, 1.554e-1], [15.0, 2.453e-1], [20.0, 3.383e-1], [30.0, 5.293e-1],
])

# materiais (η: densidade eletrônica relativa à água). Fatores relativos à água, por elétron
#            η_ref,  ρ/η,   pares, colisão, radiativo, X0 (g/cm²)
MATERIAIS = {
    "ar":      (0.001, 1.112, 1.03, 0.99, 1.05, 36.62),
    "pulmao":  (0.26,  1.003, 1.00, 1.00, 1.00, 36.39),
    "tecido":  (1.00,  1.000, 1.00, 1.00, 1.00, 36.08),
    "osso":    (1.70,  1.083, 1.52, 0.955, 1.63, 21.0),
}


def klein_nishina(e_mev):
    """Seção de choque Compton total por elétron (cm²), Klein–Nishina."""
    k = np.asarray(e_mev, float) / ME
    t1 = (1 + k) / k ** 2 * (2 * (1 + k) / (1 + 2 * k) - np.log(1 + 2 * k) / k)
    t2 = np.log(1 + 2 * k) / (2 * k) - (1 + 3 * k) / (1 + 2 * k) ** 2
    return 2 * np.pi * RE ** 2 * (t1 + t2)


def mu_agua(e_mev):
    """Atenuação total da água (cm²/g ≡ cm⁻¹ em ρ = 1), XCOM log-log, estendida até 25 MeV."""
    e = np.asarray(e_mev, float)
    tab = _WATER
    return np.exp(np.interp(np.log(e), np.log(tab[:, 0]), np.log(tab[:, 1])))


def _delta_densidade(beta_gamma):
    x = np.log10(np.maximum(beta_gamma, 1e-30))
    cbar, x0, x1, a, m = 3.5017, 0.2400, 2.8004, 0.09116, 3.4773
    return np.where(x >= x1, 2 * np.log(10) * x - cbar,
                    np.where(x >= x0, 2 * np.log(10) * x - cbar + a * (x1 - x) ** m, 0.0))


def colisao_restrito(t_mev, corte_mev=None):
    """Poder de freamento de colisão da água para elétrons (MeV cm²/g), restrito às perdas < corte (Møller).
    corte None ou ≥ T/2: irrestrito."""
    t = np.asarray(t_mev, float)
    tau = t / ME
    g = tau + 1
    b2 = 1 - 1 / g ** 2
    bg = np.sqrt(b2) * g
    dlt = _delta_densidade(bg)
    i = I_AGUA / ME
    k = 0.153537 * ZA_AGUA
    if corte_mev is None:
        d = tau / 2
    else:
        d = np.minimum(np.asarray(corte_mev, float) / ME, tau / 2)
    # ICRU 37 (Seltzer & Berger), restrito com Δ = d (em unidades de mc²)
    f = (-1 - b2 + np.log((tau - d) * d) + tau / (tau - d)
         + (d ** 2 / 2 + (2 * tau + 1) * np.log(1 - d / tau)) / g ** 2)
    return k / b2 * (np.log(2 * (tau + 2) / i ** 2) + f - dlt)


def radiativo(t_mev):
    t = np.asarray(t_mev, float)
    return np.exp(np.interp(np.log(t), np.log(_RAD_AGUA[:, 0]), np.log(_RAD_AGUA[:, 1])))


@dataclass
class Tabelas:
    """Tabelas da água num grid log de energia, prontas para o transporte (numba)."""
    lne0: float
    dlne: float
    n: int
    mu_compton: np.ndarray     # cm⁻¹ em água (ρ = 1)
    mu_pares: np.ndarray
    mu_resto: np.ndarray       # fotoelétrico + Rayleigh (tratado como absorção local)
    s_col: np.ndarray          # MeV/cm (água), colisão restrita a < corte_delta
    s_rad: np.ndarray          # MeV/cm (água)
    sigma_delta: np.ndarray    # cm⁻¹ (água): produção de δ com energia > corte_delta
    alcance: np.ndarray        # cm (água), CSDA com colisão restrita + radiativo
    corte_delta: float
    e_min: float
    e_max: float


def tabelas(corte_delta: float = 0.2, e_min: float = 0.01, e_max: float = 25.0, n: int = 400) -> Tabelas:
    e = np.geomspace(e_min, e_max, n)
    mu_tot = mu_agua(np.clip(e, _WATER[0, 0], None))
    mc = klein_nishina(e) * NE_AGUA
    mp = np.where(e > 2 * ME, np.maximum(mu_tot - mc, 0.0), 0.0)
    mp = np.where(e > 1.5, mp, np.where(e > 2 * ME, mp * np.clip((e - 2 * ME) / (1.5 - 2 * ME), 0, 1), 0.0))
    resto = np.where(e < 1.0, np.maximum(mu_tot - mc, 0.0), 0.0)
    mc = np.where(e < _WATER[0, 0], mu_tot, mc)           # abaixo da tabela: tudo vira Compton/absorção
    s_col = colisao_restrito(e, np.full_like(e, corte_delta))
    s_rad = radiativo(e)
    tau = e / ME
    b2 = 1 - 1 / (tau + 1) ** 2
    tmax = e / 2                                           # Møller: o δ é o elétron de menor energia
    sig = np.where(tmax > corte_delta, 0.153537 * ZA_AGUA / b2 * (1 / corte_delta - 1 / np.maximum(tmax, 1e-12)), 0.0)
    # alcance CSDA (perdas contínuas restritas + radiativas), integrado em log
    s_tot = s_col + s_rad
    alc = np.zeros(n)
    alc[1:] = np.cumsum(0.5 * (e[1:] / s_tot[1:] + e[:-1] / s_tot[:-1]) * np.diff(np.log(e)))
    alc += e[0] / s_tot[0] / 2
    return Tabelas(float(np.log(e_min)), float(np.log(e_max / e_min) / (n - 1)), n, mc, mp, resto, s_col, s_rad,
                   sig, alc, corte_delta, e_min, e_max)


def material_de_eta(eta: np.ndarray) -> dict:
    """Fatores por voxel (interpolados entre materiais vizinhos em η): ρ, pares, colisão, radiativo, X0."""
    refs = [MATERIAIS[k] for k in ("ar", "pulmao", "tecido", "osso")]
    et = np.array([r[0] for r in refs])
    out = {}
    for nome, col in (("rho_por_eta", 1), ("f_pares", 2), ("f_col", 3), ("f_rad", 4), ("x0", 5)):
        out[nome] = np.interp(eta, et, np.array([r[col] for r in refs])).astype(np.float64)
    return out
