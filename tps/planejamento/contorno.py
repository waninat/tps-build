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


def corpo_3d(hu: np.ndarray, limiar_hu: float = -350.0, passo_mm: tuple | None = None) -> np.ndarray:
    """BODY em todos os cortes [nz, ny, nx], conexo em 3D.

    Corte a corte, o maior componente perdia uma das pernas (e, nos pés, podia pegar a mesa). Aqui: limiar + abertura
    leve por corte; componentes 3D; fica o maior (o paciente — as pernas se ligam na pelve) e os outros grandes
    (≥ 5% dele: pernas/braços que saem do campo de visão), menos os finos e largos (mesa, colchão: espessura < 2,5
    cm); buracos preenchidos por corte (ar intestinal e pulmão ficam dentro)."""
    nz = hu.shape[0]
    m = np.empty(hu.shape, bool)
    for k in range(nz):
        m[k] = ndimage.binary_opening(hu[k] > limiar_hu, iterations=2)
    lab, n = ndimage.label(m, structure=ndimage.generate_binary_structure(3, 1))
    if n == 0:
        return m
    tam = np.bincount(lab.ravel(), minlength=n + 1)
    tam[0] = 0
    maior = int(np.argmax(tam))
    dy = float(passo_mm[1]) if passo_mm else 1.0
    manter = [maior]
    objs = ndimage.find_objects(lab)
    for i in np.argsort(tam)[::-1][1:20]:
        if i == 0 or tam[i] < 0.05 * tam[maior]:
            break
        sl = objs[i - 1]
        espessura_mm = (sl[1].stop - sl[1].start) * dy
        if espessura_mm >= 25.0:                          # mesa/colchão: placas finas — fora
            manter.append(int(i))
    m = np.isin(lab, manter)
    del lab
    for k in range(nz):
        if m[k].any():
            m[k] = ndimage.binary_fill_holes(m[k])
    return m


def sem_aninhados(polys: list) -> list:
    """Tira os polígonos que estão dentro de outro do mesmo corte (contorno externo não tem furo: evita contorno
    duplicado/encaixado no mesmo corte)."""
    from matplotlib.path import Path as _P
    if len(polys) < 2:
        return polys
    caminhos = [_P(p[:, :2]) for p in polys]
    out = []
    for i, p in enumerate(polys):
        dentro = any(j != i and caminhos[j].contains_point(p[0, :2]) and len(polys[j]) >= len(p)
                     for j in range(len(polys)))
        if not dentro:
            out.append(p)
    return out


def pulmoes_automaticos(hu: np.ndarray, corpo: np.ndarray, x: np.ndarray, y: np.ndarray, dz_mm: float,
                        limiar_hu: float = -400.0, vol_min_cc: float = 30.0):
    """Pulmões por limiar dentro do corpo: (esquerdo, direito) [nz, ny, nx] para decúbito HFS (esquerda = +x).

    Tira a traqueia/brônquios principais (ar tubular perto da linha média) e o ar intestinal (componentes pequenos
    ou abaixo do diafragma, separados dos pulmões)."""
    dx, dy = float(abs(x[1] - x[0])), float(abs(y[1] - y[0]))
    voxel_cc = dx * dy * dz_mm / 1000.0
    dentro = ndimage.binary_erosion(corpo, structure=np.ones((1, 3, 3)), iterations=2)
    ar = (hu < limiar_hu) & dentro
    ar = ndimage.binary_opening(ar, structure=np.ones((1, 3, 3)))
    lab, n = ndimage.label(ar)
    if n == 0:
        return np.zeros_like(ar), np.zeros_like(ar)
    tam = ndimage.sum(ar, lab, range(1, n + 1)) * voxel_cc
    grandes = [i + 1 for i in np.argsort(tam)[::-1][:3] if tam[i] >= vol_min_cc]
    m = np.isin(lab, grandes)
    # traqueia: em cada corte, componentes pequenos (< 7 cm²) perto da linha média do corpo
    xc = np.array([x[np.nonzero(corpo[k].any(axis=0))[0]].mean() if corpo[k].any() else 0.0
                   for k in range(corpo.shape[0])])
    for k in range(m.shape[0]):
        if not m[k].any():
            continue
        lk, nk = ndimage.label(m[k])
        for j in range(1, nk + 1):
            cc = lk == j
            area = cc.sum() * dx * dy / 100.0
            cxj = x[np.nonzero(cc.any(axis=0))[0]].mean()
            if area < 7.0 and abs(cxj - xc[k]) < 25.0:
                m[k][cc] = False
    m = ndimage.binary_closing(m, structure=np.ones((1, 5, 5)))
    m = np.stack([ndimage.binary_fill_holes(m[k]) for k in range(m.shape[0])]) & dentro
    X = np.broadcast_to(x[None, None, :], m.shape)
    esq = m & (X > xc[:, None, None])
    dire = m & (X <= xc[:, None, None])
    return esq, dire


def ossos_automaticos(hu: np.ndarray, corpo: np.ndarray, limiar_hu: float = 200.0) -> np.ndarray:
    """Osso por limiar (cortical + trabecular denso), sem ruído pequeno e com a medular preenchida por corte."""
    m = (hu > limiar_hu) & corpo
    m = ndimage.binary_opening(m, structure=np.ones((1, 2, 2)))
    m = ndimage.binary_closing(m, structure=np.ones((1, 3, 3)))
    return np.stack([ndimage.binary_fill_holes(m[k]) for k in range(m.shape[0])])
