"""Operações de estruturas (na grade de dose): margem simétrica ou assimétrica, booleanas, anel e isodose.

Margens assimétricas como no Eclipse: em cada octante a expansão é um elipsoide com os semieixos da margem de cada
direção. Contração é a expansão do complemento com o núcleo refletido. Direções no paciente em decúbito dorsal
cabeça primeiro (HFS): direita = −x, esquerda = +x, anterior = −y, posterior = +y, inferior = −z, superior = +z.
"""
from __future__ import annotations

import numpy as np
from scipy.signal import fftconvolve

DIRECOES = ("direita", "esquerda", "anterior", "posterior", "inferior", "superior")


def _nucleo(m: dict, passo_zyx) -> np.ndarray:
    dz, dy, dx = (float(abs(v)) for v in passo_zyx)
    mx_n, mx_p = m["direita"], m["esquerda"]
    my_n, my_p = m["anterior"], m["posterior"]
    mz_n, mz_p = m["inferior"], m["superior"]
    nx = int(np.ceil(max(mx_n, mx_p) / dx)) if max(mx_n, mx_p) > 0 else 0
    ny = int(np.ceil(max(my_n, my_p) / dy)) if max(my_n, my_p) > 0 else 0
    nz = int(np.ceil(max(mz_n, mz_p) / dz)) if max(mz_n, mz_p) > 0 else 0
    Z, Y, X = np.meshgrid(np.arange(-nz, nz + 1) * dz, np.arange(-ny, ny + 1) * dy, np.arange(-nx, nx + 1) * dx,
                          indexing="ij")

    def termo(d, neg, pos):
        lim = np.where(d < 0, neg, pos)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(lim > 0, (d / np.where(lim > 0, lim, 1)) ** 2, np.where(np.abs(d) < 1e-9, 0.0, np.inf))
        return t
    k = termo(X, mx_n, mx_p) + termo(Y, my_n, my_p) + termo(Z, mz_n, mz_p) <= 1.0 + 1e-9
    return k.astype(np.float32)


def _dilata(mask: np.ndarray, k: np.ndarray) -> np.ndarray:
    if k.size == 1:
        return mask.copy()
    pad = [(s // 2, s // 2) for s in k.shape]
    mp = np.pad(mask.astype(np.float32), pad)
    r = fftconvolve(mp, k, mode="same")
    sl = tuple(slice(a, a + n) for (a, _), n in zip(pad, mask.shape))
    return r[sl] > 0.5


def margem(mask: np.ndarray, passo_zyx, mm: dict | float, contrair: bool = False) -> np.ndarray:
    """mm: número (isotrópica) ou {direção: mm} com as 6 direções (faltantes = 0)."""
    m = {d: float(mm) for d in DIRECOES} if np.isscalar(mm) else {d: float(mm.get(d, 0.0)) for d in DIRECOES}
    if any(v < 0 for v in m.values()):
        raise ValueError("Margens negativas: use contrair=True")
    if max(m.values()) > 50:
        raise ValueError("Margem acima de 50 mm")
    k = _nucleo(m, passo_zyx)
    if not contrair:
        return _dilata(mask, k)
    return ~_dilata(~mask, k[::-1, ::-1, ::-1])


def booleana(a: np.ndarray, b: np.ndarray, op: str) -> np.ndarray:
    if op == "uniao":
        return a | b
    if op == "intersecao":
        return a & b
    if op == "subtracao":
        return a & ~b
    if op == "xor":
        return a ^ b
    raise ValueError(f"operação desconhecida: {op}")


def anel(mask: np.ndarray, passo_zyx, interno_mm: float, externo_mm: float) -> np.ndarray:
    """Anel entre interno e externo mm a partir da superfície da estrutura (interno pode ser 0)."""
    if externo_mm <= interno_mm:
        raise ValueError("A margem externa do anel deve ser maior que a interna")
    fora = margem(mask, passo_zyx, externo_mm)
    dentro = margem(mask, passo_zyx, interno_mm) if interno_mm > 0 else mask
    return fora & ~dentro


def de_isodose(dose: np.ndarray, nivel_gy: float, limitar: np.ndarray | None = None) -> np.ndarray:
    m = dose >= nivel_gy
    return m & limitar if limitar is not None else m
