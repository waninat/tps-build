"""Núcleo de cálculo: TERMA por ray-tracing e superposição por cones colapsados. SRS-ENG-010..014.

Unidades internas: comprimentos em cm, densidade = densidade eletrônica relativa (η),
TERMA por unidade de massa (relativa), dose = energia depositada / (η · volume).
Implementação em CPU (numba) com a mesma estrutura de laços que será portada à GPU.
"""
from __future__ import annotations

import numpy as np
from numba import njit, prange

from tps.physics.data import mu_rho_water


# ----------------------------------------------------------------------------- TERMA
def terma_table(energies: np.ndarray, energy_fluence: np.ndarray, d_max_cm: float = 80.0,
                step_cm: float = 0.02) -> tuple[np.ndarray, float]:
    """T(d_rad) = Σ_E ψ_E · μ/ρ(E) · exp(-μ(E) d_rad), por unidade de energia-fluência."""
    d = np.arange(0.0, d_max_cm + step_cm, step_cm)
    mu = mu_rho_water(energies)
    T = (energy_fluence[None, :] * mu[None, :] * np.exp(-np.outer(d, mu))).sum(axis=1)
    return T.astype(np.float64), step_cm


@njit(cache=True, fastmath=True, nogil=True)
def _lerp_table(tab, step, d):
    x = d / step
    i = int(x)
    if i >= tab.shape[0] - 1:
        return tab[-1]
    f = x - i
    return tab[i] * (1.0 - f) + tab[i + 1] * f


@njit(cache=True, fastmath=True, nogil=True)
def _bilinear(img, u0, v0, du, dv, u, v):
    x = (u - u0) / du
    y = (v - v0) / dv
    nx = img.shape[1]
    ny = img.shape[0]
    if x < 0.0 or y < 0.0 or x > nx - 1 or y > ny - 1:
        return 0.0
    i = int(x)
    j = int(y)
    if i >= nx - 1:
        i = nx - 2
    if j >= ny - 1:
        j = ny - 2
    fx = x - i
    fy = y - j
    return (img[j, i] * (1 - fx) * (1 - fy) + img[j, i + 1] * fx * (1 - fy)
            + img[j + 1, i] * (1 - fx) * fy + img[j + 1, i + 1] * fx * fy)


@njit(cache=True, parallel=True, fastmath=True, nogil=True)
def terma_volume(eta, origin, spacing, src, w, u, v, sad,
                 fluence, f_u0, f_v0, f_du, f_dv, tab, tab_step, out):
    """Acumula em `out` o TERMA de um feixe/segmento.

    eta: [nz, ny, nx]; origin/spacing em cm (x, y, z); src/w/u/v no mesmo referencial;
    fluence: mapa de energia-fluência no plano do isocentro [nv, nu] (cm).
    """
    nz, ny, nx = eta.shape
    dx, dy, dz = spacing[0], spacing[1], spacing[2]
    step = 0.5 * min(dx, min(dy, dz))
    for k in prange(nz):
        for j in range(ny):
            for i in range(nx):
                px = origin[0] + i * dx
                py = origin[1] + j * dy
                pz = origin[2] + k * dz
                rx = px - src[0]
                ry = py - src[1]
                rz = pz - src[2]
                depth_w = rx * w[0] + ry * w[1] + rz * w[2]
                if depth_w <= 0.0:
                    continue
                pu = sad * (rx * u[0] + ry * u[1] + rz * u[2]) / depth_w
                pv = sad * (rx * v[0] + ry * v[1] + rz * v[2]) / depth_w
                psi = _bilinear(fluence, f_u0, f_v0, f_du, f_dv, pu, pv)
                if psi <= 0.0:
                    continue
                L = np.sqrt(rx * rx + ry * ry + rz * rz)
                # profundidade radiológica: amostragem ao longo do raio, só dentro da grade
                ex = rx / L
                ey = ry / L
                ez = rz / L
                n = int(L / step)
                drad = 0.0
                for s in range(n):
                    t = L - (s + 0.5) * step
                    qx = (src[0] + ex * t - origin[0]) / dx
                    qy = (src[1] + ey * t - origin[1]) / dy
                    qz = (src[2] + ez * t - origin[2]) / dz
                    ii = int(np.floor(qx + 0.5))
                    jj = int(np.floor(qy + 0.5))
                    kk = int(np.floor(qz + 0.5))
                    if ii < 0 or jj < 0 or kk < 0 or ii >= nx or jj >= ny or kk >= nz:
                        break
                    drad += eta[kk, jj, ii] * step
                isq = (sad / L) * (sad / L)
                out[k, j, i] += psi * isq * _lerp_table(tab, tab_step, drad)


@njit(cache=True, fastmath=True, nogil=True)
def _trace_depth(eta, origin, dx, dy, dz, src, rx, ry, rz, L, step):
    nz, ny, nx = eta.shape
    ex = rx / L
    ey = ry / L
    ez = rz / L
    n = int(L / step)
    drad = 0.0
    for s in range(n):
        t = L - (s + 0.5) * step
        ii = int(np.floor((src[0] + ex * t - origin[0]) / dx + 0.5))
        jj = int(np.floor((src[1] + ey * t - origin[1]) / dy + 0.5))
        kk = int(np.floor((src[2] + ez * t - origin[2]) / dz + 0.5))
        if ii < 0 or jj < 0 or kk < 0 or ii >= nx or jj >= ny or kk >= nz:
            break
        drad += eta[kk, jj, ii] * step
    return drad


@njit(cache=True, parallel=True, fastmath=True, nogil=True)
def radiological_depth(eta, origin, spacing, src):
    """Profundidade radiológica (cm) de cada voxel a partir de uma fonte pontual."""
    nz, ny, nx = eta.shape
    dx, dy, dz = spacing[0], spacing[1], spacing[2]
    step = 0.5 * min(dx, min(dy, dz))
    out = np.zeros((nz, ny, nx))
    for k in prange(nz):
        for j in range(ny):
            for i in range(nx):
                rx = origin[0] + i * dx - src[0]
                ry = origin[1] + j * dy - src[1]
                rz = origin[2] + k * dz - src[2]
                L = np.sqrt(rx * rx + ry * ry + rz * rz)
                out[k, j, i] = _trace_depth(eta, origin, dx, dy, dz, src, rx, ry, rz, L, step)
    return out


@njit(cache=True, parallel=True, fastmath=True, nogil=True)
def terma_hybrid(eta, origin, spacing, src, w, u, v, sad, fluence, f_u0, f_v0, f_du, f_dv,
                 tab, tab_step, depth_sector, exact_thr, e_k, out, econ):
    """TERMA de um segmento usando o mapa de profundidade do setor, com traçado exato
    onde Ψ ≥ exact_thr (campo aberto). Acumula também Ψ·(SAD/L)²·e^{-k·d} em `econ`
    (contaminação eletrônica)."""
    nz, ny, nx = eta.shape
    dx, dy, dz = spacing[0], spacing[1], spacing[2]
    step = 0.5 * min(dx, min(dy, dz))
    for k in prange(nz):
        for j in range(ny):
            for i in range(nx):
                rx = origin[0] + i * dx - src[0]
                ry = origin[1] + j * dy - src[1]
                rz = origin[2] + k * dz - src[2]
                depth_w = rx * w[0] + ry * w[1] + rz * w[2]
                if depth_w <= 0.0:
                    continue
                pu = sad * (rx * u[0] + ry * u[1] + rz * u[2]) / depth_w
                pv = sad * (rx * v[0] + ry * v[1] + rz * v[2]) / depth_w
                psi = _bilinear(fluence, f_u0, f_v0, f_du, f_dv, pu, pv)
                if psi <= 0.0:
                    continue
                L = np.sqrt(rx * rx + ry * ry + rz * rz)
                if psi >= exact_thr:
                    drad = _trace_depth(eta, origin, dx, dy, dz, src, rx, ry, rz, L, step)
                else:
                    drad = depth_sector[k, j, i]
                isq = (sad / L) * (sad / L)
                out[k, j, i] += psi * isq * _lerp_table(tab, tab_step, drad)
                if e_k > 0.0 and eta[k, j, i] > 0.05:
                    econ[k, j, i] += psi * isq * np.exp(-e_k * drad)


# ------------------------------------------------------------------- superposição
@njit(cache=True, fastmath=True, nogil=True)
def _dep_terms(P, T, A, a, t):
    """Retorna (contribuição de dose, novo P) para um termo exponencial no passo radiológico t."""
    at = a * t
    if at < 1e-4:
        e1 = at - 0.5 * at * at          # 1 - e^{-at}
        self_term = T * A * t * t * 0.5  # T·(A/a)·(t - (1-e^{-at})/a)
    else:
        e1 = 1.0 - np.exp(-at)
        self_term = T * (A / a) * (t - e1 / a)
    dep = P * e1 / a + self_term
    P_new = P * (1.0 - e1) + T * (A / a) * e1
    return dep, P_new


@njit(cache=True, parallel=True, fastmath=True, nogil=True)
def _transport_direction(eta, terma, spacing, g, A, a, B, b, acc, hits):
    nz, ny, nx = eta.shape
    sp = (spacing[0], spacing[1], spacing[2])
    dims = (nx, ny, nz)
    ga = (abs(g[0]), abs(g[1]), abs(g[2]))
    # eixo dominante
    ax = 0
    if ga[1] > ga[ax]:
        ax = 1
    if ga[2] > ga[ax]:
        ax = 2
    o1 = (ax + 1) % 3
    o2 = (ax + 2) % 3
    # passo: avança 1 voxel no eixo dominante
    ds = sp[ax] / ga[ax]                      # comprimento geométrico do passo (cm)
    st = (g[0] * ds / sp[0], g[1] * ds / sp[1], g[2] * ds / sp[2])  # passo em índices
    nsteps = dims[ax]
    # faixa de inícios nos eixos secundários para cobrir toda a grade
    ext1 = int(abs(st[o1]) * nsteps) + 1
    ext2 = int(abs(st[o2]) * nsteps) + 1
    start_ax = 0 if g[ax] > 0 else dims[ax] - 1
    n1 = dims[o1] + ext1
    n2 = dims[o2] + ext2
    off1 = -ext1 if st[o1] > 0 else 0
    off2 = -ext2 if st[o2] > 0 else 0
    for l1 in prange(n1):
        for l2 in range(n2):
            q1 = float(l1 + off1)
            q2 = float(l2 + off2)
            # faixa de passos em que a linha está dentro da grade nos eixos secundários
            s_lo = 0
            s_hi = nsteps - 1
            for (q0, stp, dim) in ((q1, st[o1], dims[o1]), (q2, st[o2], dims[o2])):
                if abs(stp) < 1e-12:
                    if q0 < -0.5 or q0 >= dim - 0.5:
                        s_hi = -1
                else:
                    sa = (-0.5 - q0) / stp
                    sb = (dim - 0.5 - q0) / stp
                    lo = min(sa, sb)
                    hi = max(sa, sb)
                    lo_i = int(np.ceil(lo - 1e-9))
                    hi_i = int(np.floor(hi - 1e-9))
                    if lo_i > s_lo:
                        s_lo = lo_i
                    if hi_i < s_hi:
                        s_hi = hi_i
            if s_hi < s_lo:
                continue
            PA = 0.0
            PB = 0.0
            for s in range(s_lo, s_hi + 1):
                qa = start_ax + s * st[ax]
                qb = q1 + s * st[o1]
                qc = q2 + s * st[o2]
                ia = int(np.floor(qa + 0.5))
                ib = int(np.floor(qb + 0.5))
                ic = int(np.floor(qc + 0.5))
                if ax == 0:
                    ii = ia
                    jj = ib
                    kk = ic
                elif ax == 1:
                    jj = ia
                    kk = ib
                    ii = ic
                else:
                    kk = ia
                    ii = ib
                    jj = ic
                if ii < 0 or jj < 0 or kk < 0 or ii >= nx or jj >= ny or kk >= nz:
                    continue
                e = eta[kk, jj, ii]
                T = terma[kk, jj, ii]
                hits[kk, jj, ii] += 1.0
                if e <= 0.0 and T <= 0.0:
                    continue                      # ar: energia segue sem atenuar nem depositar
                t = e * ds
                dA, PA = _dep_terms(PA, T, A, a, t)
                dB, PB = _dep_terms(PB, T, B, b, t)
                if t > 1e-6:
                    acc[kk, jj, ii] += (dA + dB) / t


@njit(cache=True, parallel=True, nogil=True)
def _accumulate(dose, acc, hits):
    nz, ny, nx = dose.shape
    for k in prange(nz):
        for j in range(ny):
            for i in range(nx):
                h = hits[k, j, i]
                if h > 0:
                    dose[k, j, i] += acc[k, j, i] / h
                acc[k, j, i] = 0.0
                hits[k, j, i] = 0.0


# permutações do array [z, y, x] que deixam o eixo dominante como último (contíguo na memória)
# (transposição, ordem dos eixos do referencial interno (x', y', z') em termos dos eixos originais)
_PERMS = {
    0: ((0, 1, 2), (0, 1, 2)),     # dominante x: sem mudança
    1: ((0, 2, 1), (1, 0, 2)),     # dominante y: array (z, x, y) → x'=y, y'=x, z'=z
    2: ((1, 2, 0), (2, 0, 1)),     # dominante z: array (y, x, z) → x'=z, y'=x, z'=y
}


def superpose(eta: np.ndarray, terma: np.ndarray, spacing_cm, beam_dir: np.ndarray,
              cones, beam_u: np.ndarray, beam_v: np.ndarray) -> np.ndarray:
    """Superposição CCC para um conjunto de TERMA com eixo de feixe `beam_dir` (x, y, z).

    Para eficiência de cache, cada direção é transportada numa cópia transposta em que o
    eixo dominante da direção é o eixo contíguo; os cálculos internos são em float32.
    """
    R = np.stack([beam_u, beam_v, beam_dir], axis=1)    # colunas: base do feixe
    sp = np.asarray(spacing_cm, np.float64)
    dirs = [R @ d for d in cones.directions]
    groups = {0: [], 1: [], 2: []}
    for m, g in enumerate(dirs):
        groups[int(np.argmax(np.abs(g)))].append(m)
    dose = np.zeros(terma.shape, np.float64)
    for axis, ms in groups.items():
        if not ms:
            continue
        tr, order = _PERMS[axis]
        eta_p = np.ascontiguousarray(eta.transpose(tr), dtype=np.float32)
        ter_p = np.ascontiguousarray(terma.transpose(tr), dtype=np.float32)
        acc = np.zeros(eta_p.shape, np.float32)
        hits = np.zeros(eta_p.shape, np.float32)
        dose_p = np.zeros(eta_p.shape, np.float64)
        sp_p = np.array([sp[order[0]], sp[order[1]], sp[order[2]]])
        for m in ms:
            g = dirs[m]
            g_p = np.array([g[order[0]], g[order[1]], g[order[2]]])
            _transport_direction(eta_p, ter_p, sp_p, g_p, cones.A[m], cones.a[m], cones.B[m], cones.b[m],
                                 acc, hits)
            _accumulate(dose_p, acc, hits)
        dose += dose_p.transpose(np.argsort(tr))
    return dose


@njit(cache=True, nogil=True)
def _cobertura(lo, hi, c, d):
    """Fração do intervalo [c − d/2, c + d/2] dentro de (lo, hi)."""
    a = max(lo, c - 0.5 * d)
    b = min(hi, c + 0.5 * d)
    return max(0.0, b - a) / d


@njit(cache=True, nogil=True)
def abertura_analitica(us, du, vs, valid, a, b, t_row, jy0, jy1, usa_jy, jx0, jx1, usa_jx, t_jaw, out):
    """Transmissão da abertura em sub-linhas `vs` (out [len(vs), len(us)]), com a cobertura exata de cada
    pixel na direção das lâminas (u) — equivale à sobreamostragem infinita em u da função `aperture`."""
    for i in range(vs.shape[0]):
        fy = 1.0
        if usa_jy:
            fy = 1.0 if (vs[i] > jy0 and vs[i] < jy1) else t_jaw
        for j in range(us.shape[0]):
            if valid[i]:
                f = _cobertura(a[i], b[i], us[j], du)
                T = f + (1.0 - f) * t_row[i]
            else:
                T = 1.0
            if usa_jx:
                fx = _cobertura(jx0, jx1, us[j], du)
                T *= fx + (1.0 - fx) * t_jaw
            out[i, j] = T * fy
