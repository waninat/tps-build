"""Pós-processamento de contornos (como as ferramentas do Eclipse): suavizar, fechar, abrir, preencher buracos,
remover ilhas e manter só a maior parte. Tudo na grade do CT, em mm (espaçamento anisotrópico). SRS-CNT-020.

A suavização é 3D (gaussiana) e preserva o volume: o nível de corte do campo suavizado é escolhido para que o volume
final seja o original. Os polígonos saem do campo contínuo (não da máscara em degraus), por isso ficam lisos.
"""
from __future__ import annotations

import numpy as np
from contourpy import contour_generator
from scipy import ndimage


def _bola(raio_mm: float, passo: tuple) -> np.ndarray:
    r = [max(0, int(np.ceil(raio_mm / p))) for p in passo]
    z, y, x = np.ogrid[-r[0]:r[0] + 1, -r[1]:r[1] + 1, -r[2]:r[2] + 1]
    return (z * passo[0]) ** 2 + (y * passo[1]) ** 2 + (x * passo[2]) ** 2 <= raio_mm ** 2 + 1e-9


def preencher(m: np.ndarray) -> np.ndarray:
    """Buracos fechados em cada corte axial e em 3D."""
    out = ndimage.binary_fill_holes(m)
    for k in range(out.shape[0]):
        if out[k].any():
            out[k] = ndimage.binary_fill_holes(out[k])
    return out


def remover_ilhas(m: np.ndarray, min_cc: float, voxel_cc: float, so_maior: bool = False) -> np.ndarray:
    lab, n = ndimage.label(m, structure=np.ones((3, 3, 3)))
    if n <= 1:
        return m
    tam = ndimage.sum(np.ones_like(lab), lab, index=np.arange(1, n + 1))
    if so_maior:
        return lab == (int(np.argmax(tam)) + 1)
    manter = np.nonzero(tam * voxel_cc >= min_cc)[0] + 1
    if manter.size == 0:
        return lab == (int(np.argmax(tam)) + 1)
    return np.isin(lab, manter)


def campo_suave(m: np.ndarray, passo: tuple, sigma_mm: float) -> tuple[np.ndarray, float]:
    """(campo contínuo suavizado, nível que preserva o volume)."""
    f = m.astype(np.float32)
    if sigma_mm <= 0:
        return f, 0.5
    s = [sigma_mm / p for p in passo]
    f = ndimage.gaussian_filter(f, s, mode="constant")
    alvo = float(m.sum())
    lo, hi = 0.2, 0.8
    for _ in range(25):                           # busca do nível que mantém o volume
        t = 0.5 * (lo + hi)
        if float((f >= t).sum()) > alvo:
            lo = t
        else:
            hi = t
    return f, float(0.5 * (lo + hi))


def processar(m: np.ndarray, passo: tuple, voxel_cc: float, suavizar_mm: float = 0.0, fechar_mm: float = 0.0,
              abrir_mm: float = 0.0, encher: bool = False, ilhas_cc: float = 0.0, so_maior: bool = False):
    """Aplica as operações na ordem: fechar → abrir → preencher → ilhas → suavizar. Devolve (campo, nível)."""
    m = m.astype(bool)
    if fechar_mm > 0:
        b = _bola(fechar_mm, passo)
        pad = [(s // 2 + 1,) * 2 for s in b.shape]
        m = ndimage.binary_closing(np.pad(m, pad), structure=b)[tuple(slice(p[0], -p[0]) for p in pad)]
    if abrir_mm > 0:
        m = ndimage.binary_opening(m, structure=_bola(abrir_mm, passo))
    if encher:
        m = preencher(m)
    if ilhas_cc > 0 or so_maior:
        m = remover_ilhas(m, ilhas_cc, voxel_cc, so_maior)
    if not m.any():
        raise ValueError("A estrutura ficou vazia com essas opções")
    return campo_suave(m, passo, suavizar_mm)


def poligonos(campo2d: np.ndarray, nivel: float, x: np.ndarray, y: np.ndarray, z: float,
              min_pontos: int = 6, min_area_mm2: float = 2.0) -> list[np.ndarray]:
    """Polígonos fechados [n, 3] no nível dado de um corte do campo contínuo."""
    if campo2d.max() < nivel:
        return []
    f = np.pad(campo2d, 1)
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    cg = contour_generator(np.r_[x[0] - dx, x, x[-1] + dx], np.r_[y[0] - dy, y, y[-1] + dy], f)
    out = []
    for ln in cg.lines(nivel):
        if len(ln) < min_pontos:
            continue
        if np.allclose(ln[0], ln[-1]):
            ln = ln[:-1]
        area = 0.5 * abs(np.dot(ln[:, 0], np.roll(ln[:, 1], 1)) - np.dot(ln[:, 1], np.roll(ln[:, 0], 1)))
        if area < min_area_mm2:
            continue
        out.append(np.c_[np.round(ln, 2), np.full(len(ln), round(float(z), 2))])
    return out
