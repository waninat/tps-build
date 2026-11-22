"""Contornos por corte do CT (polígonos em mm) e ferramentas de edição. SRS-CON-001..010.

A geometria de cada estrutura é a lista de polígonos [n, 3] nos planos do CT, como no RTSTRUCT. Para editar, os
polígonos de um corte viram uma máscara na resolução do CT (preenchimento por XOR: buracos e ilhas), a ferramenta
age na máscara, e a máscara volta a polígonos. A máscara 3D na grade de dose é sempre refeita a partir dos polígonos.
"""
from __future__ import annotations

import numpy as np
from contourpy import contour_generator
from matplotlib.path import Path as MPath
from scipy import ndimage


def poligonos_do_corte(contornos: list, z: float, tol: float) -> list[np.ndarray]:
    return [c for c in contornos if abs(float(c[0, 2]) - z) <= tol]


def mascara_de_poligonos(polys: list, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Máscara [ny, nx] (centros de pixel x, y em mm) — XOR dos polígonos (como o RTSTRUCT)."""
    m = np.zeros((len(y), len(x)), bool)
    if not polys:
        return m
    X, Y = np.meshgrid(x, y)
    pts = np.c_[X.ravel(), Y.ravel()]
    for c in polys:
        c = np.asarray(c, float)
        if len(c) < 3:
            continue
        x0, x1, y0, y1 = c[:, 0].min(), c[:, 0].max(), c[:, 1].min(), c[:, 1].max()
        ix = np.where((x >= x0) & (x <= x1))[0]
        iy = np.where((y >= y0) & (y <= y1))[0]
        if not len(ix) or not len(iy):
            continue
        sub = (slice(iy[0], iy[-1] + 1), slice(ix[0], ix[-1] + 1))
        Xs, Ys = np.meshgrid(x[sub[1]], y[sub[0]])
        dentro = MPath(c[:, :2]).contains_points(np.c_[Xs.ravel(), Ys.ravel()]).reshape(Xs.shape)
        m[sub] ^= dentro
    del pts
    return m


def poligonos_de_mascara(m: np.ndarray, x: np.ndarray, y: np.ndarray, z: float, suavizar: float = 0.0,
                         min_pontos: int = 4) -> list[np.ndarray]:
    """Polígonos fechados [n, 3] da máscara (nível 0,5 de uma versão levemente suavizada)."""
    if not m.any():
        return []
    f = np.pad(m.astype(np.float32), 1)
    if suavizar > 0:
        f = ndimage.gaussian_filter(f, suavizar)
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    xp = np.r_[x[0] - dx, x, x[-1] + dx]
    yp = np.r_[y[0] - dy, y, y[-1] + dy]
    cg = contour_generator(xp, yp, f)
    out = []
    for ln in cg.lines(0.5):
        if len(ln) < min_pontos:
            continue
        if np.allclose(ln[0], ln[-1]):
            ln = ln[:-1]
        out.append(np.c_[np.round(ln, 2), np.full(len(ln), round(float(z), 2))])
    return out


def pincel(m: np.ndarray, x: np.ndarray, y: np.ndarray, pontos: list, raio_mm: float, apagar: bool) -> np.ndarray:
    """Traço de pincel (círculos ao longo da linha dos pontos)."""
    m = m.copy()
    if not pontos:
        return m
    p = np.asarray(pontos, float)
    # densifica o traço a cada 1/3 do raio
    dens = [p[0]]
    for a, b in zip(p[:-1], p[1:]):
        n = max(1, int(np.ceil(np.linalg.norm(b - a) / max(raio_mm / 3, 0.3))))
        dens += [a + (b - a) * t for t in np.linspace(0, 1, n + 1)[1:]]
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    for cx, cy in dens:
        ix = slice(max(0, int((cx - raio_mm - x[0]) / dx) - 1), min(len(x), int((cx + raio_mm - x[0]) / dx) + 2))
        iy = slice(max(0, int((cy - raio_mm - y[0]) / dy) - 1), min(len(y), int((cy + raio_mm - y[0]) / dy) + 2))
        X, Y = np.meshgrid(x[ix], y[iy])
        disco = (X - cx) ** 2 + (Y - cy) ** 2 <= raio_mm ** 2
        if apagar:
            m[iy, ix] &= ~disco
        else:
            m[iy, ix] |= disco
    return m


def poligono(m: np.ndarray, x: np.ndarray, y: np.ndarray, pontos: list, apagar: bool) -> np.ndarray:
    p = np.asarray(pontos, float)
    if len(p) < 3:
        raise ValueError("Polígono precisa de pelo menos 3 pontos")
    novo = mascara_de_poligonos([np.c_[p, np.zeros(len(p))]], x, y)
    return (m & ~novo) if apagar else (m | novo)


def interpolar(cortes: dict, zs: np.ndarray) -> dict:
    """Preenche os cortes vazios entre cortes contornados por interpolação da distância com sinal.
    cortes: {índice do corte: máscara 2D}. Devolve só os cortes novos."""
    feitos = sorted(k for k, m in cortes.items() if m.any())
    novos = {}
    for a, b in zip(feitos[:-1], feitos[1:]):
        if b - a < 2:
            continue
        da = _dist_sinal(cortes[a])
        db = _dist_sinal(cortes[b])
        for k in range(a + 1, b):
            t = (zs[k] - zs[a]) / (zs[b] - zs[a])
            m = (1 - t) * da + t * db <= 0
            if m.any():
                novos[k] = m
    return novos


def _dist_sinal(m: np.ndarray) -> np.ndarray:
    fora = ndimage.distance_transform_edt(~m)
    dentro = ndimage.distance_transform_edt(m)
    return fora - dentro


def corpo_automatico(hu2d: np.ndarray, limiar_hu: float = -350.0) -> np.ndarray:
    """Contorno externo de um corte: maior componente acima do limiar, com buracos preenchidos
    (ar intestinal e pulmão ficam dentro)."""
    m = hu2d > limiar_hu
    m = ndimage.binary_opening(m, iterations=2)
    lab, n = ndimage.label(m)
    if n == 0:
        return m
    tam = ndimage.sum(m, lab, range(1, n + 1))
    m = lab == (int(np.argmax(tam)) + 1)
    return ndimage.binary_fill_holes(m)
