"""Malha de superfície de uma estrutura para a vista 3D (faces dos voxels de fronteira + suavização de Taubin).

Sem dependências além de numpy/scipy: a máscara é reamostrada para ~`passo_mm`, as faces entre voxel cheio e vazio
viram dois triângulos, os vértices repetidos são fundidos e a superfície é suavizada (λ/μ, não encolhe).
"""
from __future__ import annotations

import numpy as np
from scipy import sparse
from scipy.ndimage import maximum_filter

# (eixo do índice, sinal): eixo 0 = z (k), 1 = y (j), 2 = x (i)
_DIRS = [(a, s) for a in range(3) for s in (1, -1)]


def malha(mascara: np.ndarray, eixos: tuple, passo_mm: float = 4.0, suavizar: int = 10,
          max_faces: int = 160_000) -> dict:
    """mascara[z, y, x]; eixos = (x, y, z) em mm. Devolve {"v": [x, y, z, ...] mm, "f": [i, j, k, ...]}."""
    x, y, z = (np.asarray(e, float) for e in eixos)
    m = np.asarray(mascara, bool)
    if not m.any():
        return {"v": [], "f": []}
    passos = [abs(z[1] - z[0]) if len(z) > 1 else 3.0, abs(y[1] - y[0]), abs(x[1] - x[0])]
    while True:
        f = [max(1, int(round(passo_mm / p))) for p in passos]
        mm = maximum_filter(m, size=f, mode="constant")[::f[0], ::f[1], ::f[2]] if max(f) > 1 else m
        kk, jj, ii = np.nonzero(mm)
        sl = (slice(kk.min(), kk.max() + 1), slice(jj.min(), jj.max() + 1), slice(ii.min(), ii.max() + 1))
        o = (kk.min(), jj.min(), ii.min())
        sub = np.pad(mm[sl], 1)
        n_fac = sum(int((sub & ~np.roll(sub, -s, axis=a)).sum()) for a, s in _DIRS)
        if n_fac <= max_faces or passo_mm > 20:
            break
        passo_mm *= 1.4
    eixo_mm = [z[::f[0]], y[::f[1]], x[::f[2]]]
    quads = []
    for a, s in _DIRS:
        face = sub & ~np.roll(sub, -s, axis=a)
        idx = np.argwhere(face)                         # (k, j, i) no sub com borda
        if not len(idx):
            continue
        b, c = [e for e in range(3) if e != a]
        base = idx.astype(np.int64).copy()
        if s > 0:
            base[:, a] += 1                              # face do lado + do voxel
        cs = []
        for db, dc in ((0, 0), (1, 0), (1, 1), (0, 1)):
            q = base.copy()
            q[:, b] += db
            q[:, c] += dc
            cs.append(q)
        quads.append((np.stack(cs, axis=1), a, s))     # [n, 4, 3] cantos em índice inteiro
    # coordenadas dos cantos: índice do canto − 0,5 − borda → mm (interpolação linear dos eixos)
    tudo = np.concatenate([q for q, _, _ in quads]).reshape(-1, 3)
    chave = (tudo[:, 0] * 1_000_003 + tudo[:, 1]) * 1_000_003 + tudo[:, 2]
    uniq, inv = np.unique(chave, return_inverse=True)
    cantos = np.zeros((len(uniq), 3), np.int64)
    cantos[inv] = tudo

    def para_mm(cidx, eixo, off):
        p = cidx - 1.5 + off                              # posição em índice da grade reamostrada
        n = len(eixo)
        d = (eixo[-1] - eixo[0]) / (n - 1) if n > 1 else 3.0
        return eixo[0] + p * d

    V = np.stack([para_mm(cantos[:, 2], eixo_mm[2], o[2]), para_mm(cantos[:, 1], eixo_mm[1], o[1]),
                  para_mm(cantos[:, 0], eixo_mm[0], o[0])], axis=1)
    faces = []
    k0 = 0
    for q, a, s in quads:
        n = len(q)
        ids = inv[k0:k0 + 4 * n].reshape(n, 4)
        k0 += 4 * n
        tri = np.concatenate([ids[:, [0, 1, 2]], ids[:, [0, 2, 3]]])
        # orienta para fora: normal do triângulo · direção da face no mundo
        nrm = np.cross(V[tri[0, 1]] - V[tri[0, 0]], V[tri[0, 2]] - V[tri[0, 0]])
        fora = np.zeros(3)
        # direção do índice "a" no mundo (x, y, z)
        mundo = {0: 2, 1: 1, 2: 0}[a]
        e = eixo_mm[a]
        fora[mundo] = s * (np.sign(e[-1] - e[0]) if len(e) > 1 else 1.0)
        if np.dot(nrm, fora) < 0:
            tri = tri[:, [0, 2, 1]]
        faces.append(tri)
    F = np.concatenate(faces).astype(np.int64)
    if suavizar > 0 and len(V) > 3:
        ed = np.concatenate([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]])
        A = sparse.coo_matrix((np.ones(len(ed)), (ed[:, 0], ed[:, 1])), shape=(len(V), len(V))).tocsr()
        A = ((A + A.T) > 0).astype(np.float64)
        grau = np.asarray(A.sum(axis=1)).ravel()
        grau[grau == 0] = 1
        Wm = sparse.diags(1.0 / grau) @ A
        for _ in range(int(suavizar)):
            for lam in (0.5, -0.53):
                V = V + lam * (Wm @ V - V)
    return {"v": np.round(V, 1).ravel().tolist(), "f": F.astype(np.int32).ravel().tolist(),
            "passo_mm": round(float(passo_mm), 1)}
