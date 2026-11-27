"""Transporte Monte Carlo de fótons e elétrons numa grade de voxels (numba, paralelo). SRS-MC-002.

Fótons: rastreamento de Woodcock (delta tracking) com a atenuação máxima da grade; Compton (Klein–Nishina,
amostragem de Kahn), produção de pares (energia dividida uniformemente, ângulo ~ mc²/E) e o resto (fotoelétrico,
Rayleigh) absorvido no local. Elétrons e pósitrons: história condensada classe II — perda contínua de colisão
restrita e radiativa, raios δ (Møller, 1/T²) acima do corte, bremsstrahlung emitido como fóton na direção do
elétron, espalhamento múltiplo gaussiano (Highland) com dobradiça aleatória, passo limitado pela perda de energia
(fração `f_passo`) e pelo voxel. Pósitron no fim do alcance: dois fótons de 0,511 MeV opostos.
Cada lote corre em paralelo (um bloco de histórias por thread, cada um com sua grade de energia depositada).
"""
from __future__ import annotations

import math

import numpy as np
from numba import njit, prange

ME = 0.51099895
PILHA = 256


@njit(cache=True, fastmath=True, inline="always")
def _tab(tab, lne0, dlne, e):
    x = (math.log(e) - lne0) / dlne
    if x <= 0.0:
        return tab[0]
    i = int(x)
    if i >= tab.shape[0] - 1:
        return tab[tab.shape[0] - 1]
    f = x - i
    return tab[i] * (1.0 - f) + tab[i + 1] * f


@njit(cache=True, fastmath=True, inline="always")
def _girar(u, v, w, cost, phi):
    """Gira a direção (u, v, w) de um ângulo polar (cos) e azimutal phi."""
    sint = math.sqrt(max(0.0, 1.0 - cost * cost))
    cp, sp = math.cos(phi), math.sin(phi)
    if abs(w) > 0.99999:
        s = 1.0 if w > 0 else -1.0
        return sint * cp, sint * sp, s * cost
    tmp = math.sqrt(1.0 - w * w)
    un = u * cost + sint * (u * w * cp - v * sp) / tmp
    vn = v * cost + sint * (v * w * cp + u * sp) / tmp
    wn = w * cost - sint * cp * tmp
    n = math.sqrt(un * un + vn * vn + wn * wn)
    return un / n, vn / n, wn / n


@njit(cache=True, fastmath=True, inline="always")
def _voxel(x, y, z, ox, oy, oz, dx, dy, dz, nx, ny, nz):
    ix = int(math.floor((x - ox) / dx + 0.5))
    iy = int(math.floor((y - oy) / dy + 0.5))
    iz = int(math.floor((z - oz) / dz + 0.5))
    if ix < 0 or iy < 0 or iz < 0 or ix >= nx or iy >= ny or iz >= nz:
        return -1, -1, -1
    return ix, iy, iz


@njit(cache=True, fastmath=True)
def _entrar(x, y, z, u, v, w, lo, hi):
    """Distância até entrar na caixa [lo, hi] (−1 se não cruza)."""
    t0, t1 = -1e30, 1e30
    p = (x, y, z)
    d = (u, v, w)
    for k in range(3):
        if abs(d[k]) < 1e-12:
            if p[k] < lo[k] or p[k] > hi[k]:
                return -1.0
        else:
            a = (lo[k] - p[k]) / d[k]
            b = (hi[k] - p[k]) / d[k]
            if a > b:
                a, b = b, a
            t0 = max(t0, a)
            t1 = min(t1, b)
    if t1 < max(t0, 0.0):
        return -1.0
    return max(t0, 0.0) + 1e-6


@njit(cache=True, fastmath=True)
def _historia(edep, x, y, z, u, v, w, e, eta, fpar, fcol, frad, x0cm, mu_max_eta,
              origem, passo, n3, tabs, lne0, dlne, cortes, f_passo):
    """Uma história de fóton primário com todas as secundárias (pilha local)."""
    ox, oy, oz = origem[0], origem[1], origem[2]
    dx, dy, dz = passo[0], passo[1], passo[2]
    nx, ny, nz = n3[0], n3[1], n3[2]
    mu_c, mu_p, mu_r, s_col, s_rad, sig_d = tabs[0], tabs[1], tabs[2], tabs[3], tabs[4], tabs[5]
    e_cut, p_cut, t_c = cortes[0], cortes[1], cortes[2]
    vmin = min(dx, min(dy, dz))
    pt = np.empty(PILHA, np.int64)
    px = np.empty((PILHA, 7))
    n = 1
    pt[0] = 0
    px[0, 0], px[0, 1], px[0, 2], px[0, 3], px[0, 4], px[0, 5], px[0, 6] = x, y, z, u, v, w, e
    while n > 0:
        n -= 1
        tipo = pt[n]
        x, y, z, u, v, w, e = px[n, 0], px[n, 1], px[n, 2], px[n, 3], px[n, 4], px[n, 5], px[n, 6]
        if tipo == 0:
            # ---------------- fóton (Woodcock)
            while True:
                if e < p_cut:
                    ix, iy, iz = _voxel(x, y, z, ox, oy, oz, dx, dy, dz, nx, ny, nz)
                    if ix >= 0:
                        edep[iz, iy, ix] += e
                    break
                mc = _tab(mu_c, lne0, dlne, e)
                mp = _tab(mu_p, lne0, dlne, e)
                mr = _tab(mu_r, lne0, dlne, e)
                mmax = (mc + mp * 1.6 + mr) * mu_max_eta
                s = -math.log(1.0 - np.random.random()) / mmax
                x += u * s
                y += v * s
                z += w * s
                ix, iy, iz = _voxel(x, y, z, ox, oy, oz, dx, dy, dz, nx, ny, nz)
                if ix < 0:
                    break
                et = eta[iz, iy, ix]
                m_c = mc * et
                m_p = mp * et * fpar[iz, iy, ix]
                m_r = mr * et
                r = np.random.random() * mmax
                if r >= m_c + m_p + m_r:
                    continue                                   # interação virtual
                if r < m_c:
                    # Compton (Kahn)
                    k = e / ME
                    while True:
                        r1, r2, r3 = np.random.random(), np.random.random(), np.random.random()
                        if r1 <= (1 + 2 * k) / (9 + 2 * k):
                            xx = 1 + 2 * k * r2
                            if r3 <= 4 * (1 / xx - 1 / (xx * xx)):
                                break
                        else:
                            xx = (1 + 2 * k) / (1 + 2 * k * r2)
                            ct = 1 + (1 - xx) / k
                            if r3 <= 0.5 * (ct * ct + 1 / xx):
                                break
                    ct = 1 - (xx - 1) / k
                    e1 = e / xx
                    phi = 2 * math.pi * np.random.random()
                    un, vn, wn = _girar(u, v, w, ct, phi)
                    te = e - e1
                    # elétron: momento = p_γ − p_γ'
                    pxe, pye, pze = e * u - e1 * un, e * v - e1 * vn, e * w - e1 * wn
                    pn = math.sqrt(pxe * pxe + pye * pye + pze * pze)
                    if te > 0 and n < PILHA - 1 and pn > 0:
                        pt[n] = 1
                        px[n, 0], px[n, 1], px[n, 2] = x, y, z
                        px[n, 3], px[n, 4], px[n, 5], px[n, 6] = pxe / pn, pye / pn, pze / pn, te
                        n += 1
                    elif te > 0:
                        edep[iz, iy, ix] += te
                    u, v, w, e = un, vn, wn, e1
                    continue
                if r < m_c + m_p:
                    # pares: e− e+ com a energia cinética dividida uniformemente
                    tk = e - 2 * ME
                    f = np.random.random()
                    for q in range(2):
                        t_q = tk * (f if q == 0 else 1 - f)
                        th = ME / (t_q + ME)
                        un, vn, wn = _girar(u, v, w, math.cos(th), 2 * math.pi * np.random.random())
                        if n < PILHA - 1:
                            pt[n] = 1 if q == 0 else 2
                            px[n, 0], px[n, 1], px[n, 2], px[n, 3], px[n, 4], px[n, 5], px[n, 6] = \
                                x, y, z, un, vn, wn, t_q
                            n += 1
                        else:
                            edep[iz, iy, ix] += t_q
                    break
                edep[iz, iy, ix] += e                          # fotoelétrico / Rayleigh: local
                break
            continue
        # ---------------- elétron (1) ou pósitron (2)
        rad = 0.0
        while True:
            ix, iy, iz = _voxel(x, y, z, ox, oy, oz, dx, dy, dz, nx, ny, nz)
            if ix < 0:
                break
            if e < e_cut:
                edep[iz, iy, ix] += e
                if tipo == 2 and n < PILHA - 2:              # aniquilação
                    ct = 2 * np.random.random() - 1
                    phi = 2 * math.pi * np.random.random()
                    st = math.sqrt(1 - ct * ct)
                    a1, a2, a3 = st * math.cos(phi), st * math.sin(phi), ct
                    for sg in (1.0, -1.0):
                        pt[n] = 0
                        px[n, 0], px[n, 1], px[n, 2] = x, y, z
                        px[n, 3], px[n, 4], px[n, 5], px[n, 6] = sg * a1, sg * a2, sg * a3, ME
                        n += 1
                break
            et = max(eta[iz, iy, ix], 1e-4)                   # ar do CT com η = 0: passo finito
            sc = _tab(s_col, lne0, dlne, e) * et * fcol[iz, iy, ix]
            sr = _tab(s_rad, lne0, dlne, e) * et * frad[iz, iy, ix]
            sd = _tab(sig_d, lne0, dlne, e) * et
            s_e = f_passo * e / (sc + sr)
            s = min(s_e, 0.6 * vmin)
            s_d = -math.log(1.0 - np.random.random()) / sd if sd > 0 else 1e30
            delta = s_d < s
            if delta:
                s = s_d
            if s < 1e-5:
                s = 1e-5
            # perda no meio do passo
            e_meio = max(e - 0.5 * s * (sc + sr), 0.5 * e)
            sc = _tab(s_col, lne0, dlne, e_meio) * et * fcol[iz, iy, ix]
            sr = _tab(s_rad, lne0, dlne, e_meio) * et * frad[iz, iy, ix]
            de = min(s * sc, e)
            dr = min(s * sr, e - de)
            # espalhamento múltiplo (Highland) com dobradiça aleatória
            x0l = x0cm[iz, iy, ix]
            tot = e_meio + ME
            pc = math.sqrt(e_meio * (e_meio + 2 * ME))
            beta = pc / tot
            t = s / x0l
            th0 = 13.6 / (beta * pc) * math.sqrt(t) * (1 + 0.038 * math.log(max(t, 1e-6)))
            if th0 < 0:
                th0 = 0.0
            th = th0 * math.sqrt(-2 * math.log(1.0 - np.random.random()))
            if th > math.pi:
                th = math.pi
            h = np.random.random() * s
            x += u * h
            y += v * h
            z += w * h
            u, v, w = _girar(u, v, w, math.cos(th), 2 * math.pi * np.random.random())
            jx, jy, jz = _voxel(x, y, z, ox, oy, oz, dx, dy, dz, nx, ny, nz)
            if jx < 0:
                break
            edep[jz, jy, jx] += de
            x += u * (s - h)
            y += v * (s - h)
            z += w * (s - h)
            e -= de + dr
            rad += dr
            if rad > p_cut and n < PILHA - 1:                 # bremsstrahlung na direção do elétron
                ct = math.cos(ME / (e + ME + rad))
                un, vn, wn = _girar(u, v, w, ct, 2 * math.pi * np.random.random())
                pt[n] = 0
                px[n, 0], px[n, 1], px[n, 2], px[n, 3], px[n, 4], px[n, 5], px[n, 6] = x, y, z, un, vn, wn, rad
                n += 1
                rad = 0.0
            if delta and e > 2 * t_c:
                # raio δ (Møller, termo dominante 1/T²) entre o corte e E/2
                tmax = 0.5 * e
                td = 1.0 / (1.0 / t_c - np.random.random() * (1.0 / t_c - 1.0 / tmax))
                e0 = e
                e1 = e0 - td
                ctd = math.sqrt(max(0.0, td * (e0 + 2 * ME) / (e0 * (td + 2 * ME))))
                ctp = math.sqrt(max(0.0, e1 * (e0 + 2 * ME) / (e0 * (e1 + 2 * ME))))
                phi = 2 * math.pi * np.random.random()
                if n < PILHA - 1:
                    a1, a2, a3 = _girar(u, v, w, min(ctd, 1.0), phi + math.pi)
                    pt[n] = 1
                    px[n, 0], px[n, 1], px[n, 2], px[n, 3], px[n, 4], px[n, 5], px[n, 6] = x, y, z, a1, a2, a3, td
                    n += 1
                else:
                    jx, jy, jz = _voxel(x, y, z, ox, oy, oz, dx, dy, dz, nx, ny, nz)
                    if jx >= 0:
                        edep[jz, jy, jx] += td
                u, v, w = _girar(u, v, w, min(ctp, 1.0), phi)
                e = e1
        if rad > 0:
            ix, iy, iz = _voxel(x, y, z, ox, oy, oz, dx, dy, dz, nx, ny, nz)
            if ix >= 0:
                edep[iz, iy, ix] += rad


@njit(cache=True, fastmath=True)
def _amostrar_fonte(seg_cdf, seg_ofs, seg_n, cel_cdf, cel_u, cel_v, du, dv, geo, esp_cdf, esp_lo, esp_hi):
    """Segmento, célula do mapa de fluência, posição no plano do isocentro, direção a partir da fonte e energia."""
    r = np.random.random()
    s = np.searchsorted(seg_cdf, r)
    if s >= seg_cdf.shape[0]:
        s = seg_cdf.shape[0] - 1
    o, m = seg_ofs[s], seg_n[s]
    r = np.random.random()
    k = o + np.searchsorted(cel_cdf[o:o + m], r)
    if k >= o + m:
        k = o + m - 1
    uu = cel_u[k] + (np.random.random() - 0.5) * du
    vv = cel_v[k] + (np.random.random() - 0.5) * dv
    g = geo[s]                         # src(3), U(3), V(3), W(3), sad
    sad = g[12]
    px_ = g[0] + sad * g[9] + uu * g[3] + vv * g[6]
    py_ = g[1] + sad * g[10] + uu * g[4] + vv * g[7]
    pz_ = g[2] + sad * g[11] + uu * g[5] + vv * g[8]
    ddx, ddy, ddz = px_ - g[0], py_ - g[1], pz_ - g[2]
    nn = math.sqrt(ddx * ddx + ddy * ddy + ddz * ddz)
    r = np.random.random()
    b = np.searchsorted(esp_cdf, r)
    if b >= esp_cdf.shape[0]:
        b = esp_cdf.shape[0] - 1
    e = esp_lo[b] + np.random.random() * (esp_hi[b] - esp_lo[b])
    return g[0], g[1], g[2], ddx / nn, ddy / nn, ddz / nn, e


@njit(cache=True, parallel=True)
def lote(edep, n_hist, semente, eta, fpar, fcol, frad, x0cm, mu_max_eta, origem, passo, n3, tabs, lne0, dlne,
         cortes, f_passo, seg_cdf, seg_ofs, seg_n, cel_cdf, cel_u, cel_v, du, dv, geo, esp_cdf, esp_lo, esp_hi,
         lo, hi):
    """n_hist histórias divididas entre os blocos edep[b] (um por thread)."""
    nb = edep.shape[0]
    for b in prange(nb):
        np.random.seed(semente + 7919 * b)
        nh = n_hist // nb + (1 if b < n_hist % nb else 0)
        for _ in range(nh):
            x, y, z, u, v, w, e = _amostrar_fonte(seg_cdf, seg_ofs, seg_n, cel_cdf, cel_u, cel_v, du, dv, geo,
                                                  esp_cdf, esp_lo, esp_hi)
            d = _entrar(x, y, z, u, v, w, lo, hi)
            if d < 0:
                continue
            _historia(edep[b], x + u * d, y + v * d, z + w * d, u, v, w, e, eta, fpar, fcol, frad, x0cm,
                      mu_max_eta, origem, passo, n3, tabs, lne0, dlne, cortes, f_passo)


# uma chamada paralela do numba por vez no processo (tps.engine.paralelo)
from tps.engine.paralelo import serializado  # noqa: E402

lote = serializado(lote)
