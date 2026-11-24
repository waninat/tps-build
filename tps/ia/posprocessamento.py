"""Órgãos derivados das classes da IA (não existem prontos no TotalSegmentator). SRS-CON-040..045.

Todas as funções recebem máscaras na grade do CT [nz, ny, nx] com eixos (x, y, z) em mm, z crescente para cranial.
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage


def _maior(m2: np.ndarray) -> np.ndarray:
    lab, n = ndimage.label(m2)
    if n <= 1:
        return m2
    t = ndimage.sum(m2, lab, range(1, n + 1))
    return lab == (int(np.argmax(t)) + 1)


def reto_do_colon(colon: np.ndarray, x: np.ndarray, y: np.ndarray, z: np.ndarray, zmax_mm: float | None = None,
                  comprimento_max_mm: float = 160.0) -> tuple[np.ndarray, float | None]:
    """Reto = o tubo do cólon a partir do corte mais inferior até a junção retossigmoide.

    Sobe corte a corte seguindo o componente ligado ao de baixo. Para quando: (a) a área salta para mais de 1,8× a
    mediana dos 5 cortes anteriores (o sigmoide se junta ao tubo), (b) o centro anda mais de 25 mm para anterior
    em relação ao trajeto até ali (o sigmoide vira para a frente), (c) aparecem dois pedaços ligados ao tubo, ou
    (d) passou de `comprimento_max_mm`. `zmax_mm` fixa o limite superior (ajuste do usuário). Devolve (máscara, z do
    limite superior usado)."""
    nz = colon.shape[0]
    ks = [k for k in range(nz) if colon[k].any()]
    out = np.zeros_like(colon)
    if not ks:
        return out, None
    ordem = sorted(ks, key=lambda k: z[k])
    dy = float(abs(y[1] - y[0]))
    k0 = ordem[0]
    atual = _maior(colon[k0])
    out[k0] = atual
    areas, ys = [atual.sum()], [float(y[np.nonzero(atual.any(axis=1))[0]].mean())]
    z0, zfim = float(z[k0]), float(z[k0])
    for k in ordem[1:]:
        if zmax_mm is not None and z[k] > zmax_mm:
            break
        if zmax_mm is None and z[k] - z0 > comprimento_max_mm:
            break
        viz = ndimage.binary_dilation(atual, iterations=max(1, int(round(5.0 / dy))))
        lab, n = ndimage.label(colon[k])
        ligados = [j for j in range(1, n + 1) if (viz & (lab == j)).any()]
        if not ligados:
            break
        if zmax_mm is None and len(ligados) > 1 and z[k] - z0 > 50:
            break
        m = np.isin(lab, ligados)
        a = float(m.sum())
        yc = float(y[np.nonzero(m.any(axis=1))[0]].mean())
        if zmax_mm is None and z[k] - z0 > 50:
            ref = float(np.median(areas[-5:]))
            if a > 1.8 * ref or (np.mean(ys[-5:]) - yc) > 25.0:     # y menor = anterior (HFS)
                break
        out[k] = m
        atual = m
        areas.append(a)
        ys.append(yc)
        zfim = float(z[k])
    return out, zfim


def parede(m: np.ndarray, x: np.ndarray, y: np.ndarray, espessura_mm: float = 3.0) -> np.ndarray:
    """Parede de órgão oco: o órgão menos ele mesmo erodido no plano axial."""
    it = max(1, int(round(espessura_mm / float(abs(x[1] - x[0])))))
    er = np.stack([ndimage.binary_erosion(m[k], iterations=it) if m[k].any() else m[k] for k in range(m.shape[0])])
    return m & ~er


def inferior(m: np.ndarray, z: np.ndarray, mm: float) -> np.ndarray:
    ks = np.nonzero(m.any(axis=(1, 2)))[0]
    out = np.zeros_like(m)
    if len(ks):
        z0 = min(z[ks])
        sel = z <= z0 + mm
        out[sel] = m[sel]
    return out


def lado(m: np.ndarray, corpo: np.ndarray, x: np.ndarray, esquerdo: bool) -> np.ndarray:
    """Metade esquerda (+x em HFS) ou direita de uma máscara, pela linha média do corpo em cada corte."""
    out = np.zeros_like(m)
    for k in range(m.shape[0]):
        if not m[k].any():
            continue
        cols = np.nonzero(corpo[k].any(axis=0))[0] if corpo[k].any() else np.nonzero(m[k].any(axis=0))[0]
        xc = float(x[cols].mean())
        meia = (x > xc) if esquerdo else (x <= xc)
        out[k] = m[k] & meia[None, :]
    return out


def bowel_bag(cavidade: np.ndarray, z: np.ndarray, excluir: list[np.ndarray], z_topo_alvo: float | None,
              acima_mm: float = 20.0) -> np.ndarray:
    """Bowel bag (RTOG): cavidade abdominopélvica sem bexiga, reto e osso, até `acima_mm` acima do alvo."""
    m = cavidade.copy()
    for e in excluir:
        if e is not None:
            m &= ~e
    if z_topo_alvo is not None:
        m[z > z_topo_alvo + acima_mm] = False
    return m
