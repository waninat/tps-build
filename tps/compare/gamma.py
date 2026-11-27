"""Comparação de distribuições de dose: índice gama 3D. SRS-CMP-001..003.

Implementação própria (sem dependências externas além do SciPy), no estilo de
Low et al. (1998) com busca em cascas esféricas e interpolação trilinear da
distribuição avaliada. Padrões alinhados ao AAPM TG-218: análise global,
limiar de dose baixa 10%, critérios configuráveis (ex.: 3%/2 mm).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit, prange
from scipy.interpolate import RegularGridInterpolator


@dataclass
class GammaResult:
    criteria: str
    pass_rate: float          # % de pontos avaliados com γ ≤ 1
    n_evaluated: int
    gamma: np.ndarray         # mesmo formato da referência (NaN fora da análise)
    mean_gamma: float
    p95_gamma: float


def _shell_offsets(dta: float, step: float, r_max: float, ndim: int) -> list[np.ndarray]:
    """Deslocamentos agrupados por distância crescente (cascas)."""
    n = int(np.ceil(r_max / step))
    grid = np.arange(-n, n + 1) * step
    mesh = np.stack(np.meshgrid(*([grid] * ndim), indexing="ij"), axis=-1).reshape(-1, ndim)
    dist = np.linalg.norm(mesh, axis=1)
    keep = dist <= r_max + 1e-9
    mesh, dist = mesh[keep], dist[keep]
    shells = []
    for r in np.unique(np.round(dist / step, 6)):
        shells.append(mesh[np.isclose(dist / step, r)])
    return shells


def gamma_referencia(axes_ref, dose_ref, axes_eval, dose_eval, dose_pct: float = 3.0, dta_mm: float = 2.0,
                     cutoff_pct: float = 10.0, local: bool = False, interp_fraction: int = 10,
                     max_gamma: float = 2.0, norm_dose: float | None = None) -> GammaResult:
    """Implementação vetorizada original (até a versão 0.7.1). Exata, mas a memória cresce com
    (pontos ativos × deslocamentos de uma casca): em planos grandes passa de vários GB. Fica só como
    referência para os testes de equivalência; o cálculo usa gamma_3d."""
    dose_ref = np.asarray(dose_ref, float)
    dose_eval = np.asarray(dose_eval, float)
    ndim = dose_ref.ndim
    norm = float(norm_dose if norm_dose is not None else dose_ref.max())
    interp = RegularGridInterpolator(tuple(np.asarray(a, float) for a in axes_eval), dose_eval,
                                     bounds_error=False, fill_value=np.nan)

    mesh = np.stack(np.meshgrid(*axes_ref, indexing="ij"), axis=-1)
    mask = dose_ref >= cutoff_pct / 100.0 * norm
    pts = mesh[mask]
    dref = dose_ref[mask]
    tol = dose_pct / 100.0 * (dref if local else np.full_like(dref, norm))

    best = np.full(dref.shape, np.inf)
    step = dta_mm / interp_fraction
    for shell in _shell_offsets(dta_mm, step, max_gamma * dta_mm, ndim):
        r = np.linalg.norm(shell[0])
        dist_term = (r / dta_mm) ** 2
        active = best > dist_term                 # só pontos que ainda podem melhorar
        if not np.any(active):
            break
        idx = np.where(active)[0]
        p = pts[idx][:, None, :] + shell[None, :, :]
        de = interp(p.reshape(-1, ndim)).reshape(len(idx), len(shell))
        dd = ((de - dref[idx, None]) / tol[idx, None]) ** 2
        g2 = np.nanmin(dd, axis=1) + dist_term
        best[idx] = np.minimum(best[idx], g2)

    g = np.sqrt(best)
    g[~np.isfinite(g)] = max_gamma
    g = np.minimum(g, max_gamma)
    out = np.full(dose_ref.shape, np.nan)
    out[mask] = g
    rate = float(np.mean(g <= 1.0) * 100) if g.size else float("nan")
    crit = f"{dose_pct:g}%/{dta_mm:g} mm {'local' if local else 'global'}, limiar {cutoff_pct:g}%"
    return GammaResult(crit, rate, int(g.size), out, float(np.mean(g)) if g.size else np.nan,
                       float(np.percentile(g, 95)) if g.size else np.nan)


# ----------------------------------------------------------------------------- implementação usada
def _offsets_ordenados(dta: float, step: float, r_max: float, ndim: int):
    """Deslocamentos da malha de busca dentro da esfera, em ordem crescente de distância."""
    n = int(np.ceil(r_max / step))
    grid = np.arange(-n, n + 1) * step
    mesh = np.stack(np.meshgrid(*([grid] * ndim), indexing="ij"), axis=-1).reshape(-1, ndim)
    dist = np.linalg.norm(mesh, axis=1)
    keep = dist <= r_max + 1e-9
    mesh, dist = mesh[keep], dist[keep]
    ordem = np.argsort(np.round(dist / step, 6), kind="stable")
    off3 = np.zeros((len(ordem), 3))
    off3[:, 3 - ndim:] = mesh[ordem]
    return off3, (dist[ordem] / dta) ** 2


@njit(cache=True, inline="always", nogil=True)
def _eixo(ax, c):
    """Índice inferior e peso da interpolação linear num eixo crescente; -1 fora do eixo."""
    n = ax.shape[0]
    if n == 1:
        return (0, 0.0) if abs(c - ax[0]) <= 1e-9 else (-1, 0.0)
    if c < ax[0] - 1e-9 or c > ax[n - 1] + 1e-9:
        return -1, 0.0
    i = np.searchsorted(ax, c, side="right") - 1
    if i < 0:
        i = 0
    if i > n - 2:
        i = n - 2
    f = (c - ax[i]) / (ax[i + 1] - ax[i])
    if f < 0.0:
        f = 0.0
    if f > 1.0:
        f = 1.0
    return i, f


@njit(cache=True, inline="always", nogil=True)
def _trilinear(ev, az, ay, ax, z, y, x):
    k, fz = _eixo(az, z)
    j, fy = _eixo(ay, y)
    i, fx = _eixo(ax, x)
    if k < 0 or j < 0 or i < 0:
        return np.nan
    k1 = k + 1 if az.shape[0] > 1 else k
    j1 = j + 1 if ay.shape[0] > 1 else j
    i1 = i + 1 if ax.shape[0] > 1 else i
    c00 = ev[k, j, i] * (1 - fx) + ev[k, j, i1] * fx
    c01 = ev[k, j1, i] * (1 - fx) + ev[k, j1, i1] * fx
    c10 = ev[k1, j, i] * (1 - fx) + ev[k1, j, i1] * fx
    c11 = ev[k1, j1, i] * (1 - fx) + ev[k1, j1, i1] * fx
    return (c00 * (1 - fy) + c01 * fy) * (1 - fz) + (c10 * (1 - fy) + c11 * fy) * fz


@njit(cache=True, parallel=True, nogil=True)
def _gamma_pontos(pts, dref, tol, ev, az, ay, ax, off, dterm):
    """γ² mínimo de cada ponto: percorre os deslocamentos do mais próximo ao mais distante e para quando
    o termo de distância já não pode baixar o melhor valor encontrado. Memória: só a saída."""
    n = pts.shape[0]
    out = np.full(n, np.inf)
    for p in prange(n):
        best = np.inf
        z0 = pts[p, 0]
        y0 = pts[p, 1]
        x0 = pts[p, 2]
        for m in range(off.shape[0]):
            if dterm[m] >= best:
                break
            de = _trilinear(ev, az, ay, ax, z0 + off[m, 0], y0 + off[m, 1], x0 + off[m, 2])
            if de != de:                          # fora da grade avaliada
                continue
            dd = (de - dref[p]) / tol[p]
            g2 = dd * dd + dterm[m]
            if g2 < best:
                best = g2
        out[p] = best
    return out


def gamma_3d(axes_ref, dose_ref, axes_eval, dose_eval, dose_pct: float = 3.0, dta_mm: float = 2.0,
             cutoff_pct: float = 10.0, local: bool = False, interp_fraction: int = 10,
             max_gamma: float = 2.0, norm_dose: float | None = None) -> GammaResult:
    """Índice gama (Low et al. 1998; padrões do TG-218). axes_*: tupla de eixos (mm) na mesma ordem dos índices
    do array; vale para 1D, 2D e 3D. Busca em malha de passo DTA/interp_fraction até max_gamma × DTA, com
    interpolação trilinear da distribuição avaliada (mesma definição de gamma_referencia).

    A memória não depende do desacordo entre as doses: cada ponto é resolvido num laço compilado que testa os
    deslocamentos em ordem crescente de distância e para assim que nenhum mais distante pode melhorar γ."""
    dose_ref = np.asarray(dose_ref, float)
    dose_eval = np.ascontiguousarray(dose_eval, float)
    ndim = dose_ref.ndim
    norm = float(norm_dose if norm_dose is not None else dose_ref.max())
    a_ref = [np.asarray(a, float) for a in axes_ref]
    a_ev = [np.asarray(a, float) for a in axes_eval]
    for a in a_ev:                                   # eixos crescentes (DICOM pode vir decrescente)
        if a.size > 1 and np.any(np.diff(a) <= 0) and not np.all(np.diff(a) < 0):
            return gamma_referencia(axes_ref, dose_ref, axes_eval, dose_eval, dose_pct, dta_mm, cutoff_pct,
                                    local, interp_fraction, max_gamma, norm_dose)
    ev = dose_eval
    for d, a in enumerate(a_ev):
        if a.size > 1 and a[1] < a[0]:
            a_ev[d] = a[::-1].copy()
            ev = np.flip(ev, axis=d)
    ev3 = np.ascontiguousarray(ev.reshape((1,) * (3 - ndim) + ev.shape))
    ax3 = [np.zeros(1)] * (3 - ndim) + a_ev

    mask = dose_ref >= cutoff_pct / 100.0 * norm
    idx = np.nonzero(mask)
    pts = np.zeros((idx[0].size, 3))
    for d in range(ndim):
        pts[:, 3 - ndim + d] = a_ref[d][idx[d]]
    dref = dose_ref[mask]
    tol = dose_pct / 100.0 * (dref if local else np.full_like(dref, norm))
    off, dterm = _offsets_ordenados(dta_mm, dta_mm / interp_fraction, max_gamma * dta_mm, ndim)
    best = _gamma_pontos(pts, dref, tol, ev3, ax3[0], ax3[1], ax3[2], off, dterm) if dref.size else np.zeros(0)

    g = np.sqrt(best)
    g[~np.isfinite(g)] = max_gamma
    g = np.minimum(g, max_gamma)
    out = np.full(dose_ref.shape, np.nan)
    out[mask] = g
    rate = float(np.mean(g <= 1.0) * 100) if g.size else float("nan")
    crit = f"{dose_pct:g}%/{dta_mm:g} mm {'local' if local else 'global'}, limiar {cutoff_pct:g}%"
    return GammaResult(crit, rate, int(g.size), out, float(np.mean(g)) if g.size else np.nan,
                       float(np.percentile(g, 95)) if g.size else np.nan)


# uma chamada paralela do numba por vez no processo (tps.engine.paralelo)
from tps.engine.paralelo import serializado  # noqa: E402

_gamma_pontos = serializado(_gamma_pontos)
