"""Fusão de imagens CT–CT: registro rígido (6 graus de liberdade) e deformável (demons multirresolução). SRS-IMG-040.

Uso típico: a mesma paciente com duas TCs em posições levemente diferentes ou com acessórios diferentes (outro
suporte, outra máscara, bexiga/reto diferentes). A TC principal (a do plano) é a FIXA; a secundária é a MÓVEL.

Transformação: um ponto x (mm) da TC principal vai para a secundária em  T(x) = R·(x + u(x) − c) + c + t,
com R e t do registro rígido (rotação em torno do centro c) e u o campo de deslocamentos do deformável (mm, na
grade grossa do registro, interpolado). Com T: (a) a secundária é reamostrada na grade da principal (fusão em
mistura, xadrez ou diferença); (b) as estruturas da secundária são propagadas para a principal (máscara da
secundária amostrada em T(x)).

Deformável: demons de Thirion com forças simétricas (gradientes das duas imagens), regularização gaussiana do campo
(modelo elástico simplificado) e do incremento (fluido), em 2–3 níveis de resolução, dentro do corpo. Métricas:
correlação (NCC) e erro absoluto médio em HU antes/depois, e o jacobiano do campo (dobras: det ≤ 0).

Não substitui a revisão: o resultado é conferido visualmente (xadrez/diferença) e as estruturas propagadas são
revisadas e editadas como qualquer contorno.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
from scipy import ndimage
from scipy.optimize import minimize
from scipy.spatial.transform import Rotation


@dataclass
class Grade:
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray

    @property
    def forma(self):
        return (len(self.z), len(self.y), len(self.x))

    @property
    def passo(self):
        return (float(self.z[1] - self.z[0]) if len(self.z) > 1 else 1.0, float(self.y[1] - self.y[0]),
                float(self.x[1] - self.x[0]))


@dataclass
class Registro:
    rotacao_graus: tuple = (0.0, 0.0, 0.0)        # em torno de x, y, z (convenção "xyz", extrínseca)
    translacao_mm: tuple = (0.0, 0.0, 0.0)
    centro_mm: tuple = (0.0, 0.0, 0.0)
    grade_def: Grade | None = None                # grade do campo deformável (na TC principal)
    u: np.ndarray | None = None                   # [3, nz, ny, nx] deslocamento (dx, dy, dz) em mm
    metricas: dict = field(default_factory=dict)

    @property
    def deformavel(self) -> bool:
        return self.u is not None

    def matriz(self) -> np.ndarray:
        return Rotation.from_euler("xyz", self.rotacao_graus, degrees=True).as_matrix()

    def deslocamento(self, pts: np.ndarray) -> np.ndarray:
        """u(x) nos pontos [n, 3] (mm)."""
        if self.u is None:
            return np.zeros_like(pts)
        g = self.grade_def
        idx = np.stack([(pts[:, 2] - g.z[0]) / g.passo[0], (pts[:, 1] - g.y[0]) / g.passo[1],
                        (pts[:, 0] - g.x[0]) / g.passo[2]])
        return np.stack([ndimage.map_coordinates(self.u[k], idx, order=1, mode="nearest") for k in range(3)], axis=1)

    def aplicar(self, pts: np.ndarray) -> np.ndarray:
        """T(x): pontos da principal → posição na secundária (mm)."""
        p = pts + self.deslocamento(pts)
        c = np.asarray(self.centro_mm)
        return (p - c) @ self.matriz().T + c + np.asarray(self.translacao_mm)

    def resumo(self) -> dict:
        return {"rotacao_graus": [round(float(v), 2) for v in self.rotacao_graus],
                "translacao_mm": [round(float(v), 1) for v in self.translacao_mm],
                "deformavel": self.deformavel, "metricas": self.metricas}


# ------------------------------------------------------------------------------------------------ amostragem
def _indices(vol_grade: Grade, pts: np.ndarray) -> np.ndarray:
    g = vol_grade
    return np.stack([(pts[:, 2] - g.z[0]) / g.passo[0], (pts[:, 1] - g.y[0]) / g.passo[1],
                     (pts[:, 0] - g.x[0]) / g.passo[2]])


def amostrar(vol: np.ndarray, vol_grade: Grade, pts: np.ndarray, ordem: int = 1, fora: float = -1000.0) -> np.ndarray:
    return ndimage.map_coordinates(vol, _indices(vol_grade, pts), order=ordem, mode="constant", cval=fora)


def pontos(g: Grade) -> np.ndarray:
    Z, Y, X = np.meshgrid(g.z, g.y, g.x, indexing="ij")
    return np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])


def grade_grossa(g: Grade, passo_mm: float) -> Grade:
    def eixo(a):
        if len(a) < 2:
            return a
        n = max(2, int(np.floor((a[-1] - a[0]) / passo_mm)) + 1)
        return a[0] + np.arange(n) * passo_mm * np.sign(a[1] - a[0])
    return Grade(eixo(g.x), eixo(g.y), eixo(g.z))


def reamostrar(vol: np.ndarray, vol_grade: Grade, destino: Grade, reg: Registro | None = None, ordem: int = 1,
               fora: float = -1000.0) -> np.ndarray:
    """Volume (na grade vol_grade) amostrado na grade destino, pela transformação reg (destino → vol)."""
    p = pontos(destino)
    if reg is not None:
        p = reg.aplicar(p)
    return amostrar(vol, vol_grade, p, ordem, fora).reshape(destino.forma)


def _norm(hu: np.ndarray) -> np.ndarray:
    """HU → intensidade para o registro (tecido mole com contraste, osso saturado)."""
    return np.clip((hu + 1000.0) / 2000.0, 0.0, 1.0).astype(np.float32)


def ncc(a: np.ndarray, b: np.ndarray, m: np.ndarray | None = None) -> float:
    if m is not None:
        a, b = a[m], b[m]
    a = a - a.mean()
    b = b - b.mean()
    d = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return float((a * b).sum() / d) if d > 0 else 0.0


# ------------------------------------------------------------------------------------------------ rígido
def rigido(fixa: np.ndarray, g_fixa: Grade, movel: np.ndarray, g_movel: Grade, mascara: np.ndarray | None = None,
           niveis_mm=(8.0, 4.0), progresso=None, max_rot_graus: float = 10.0) -> Registro:
    """Registro rígido (3 rotações + 3 translações) maximizando a correlação no corpo, multirresolução."""
    F, M = _norm(fixa), _norm(movel)
    corpo_f = mascara if mascara is not None else fixa > -400
    corpo_m = movel > -400
    cf = np.array([np.average(eixo, weights=corpo_f.sum(axis=tuple(i for i in range(3) if i != ax)))
                   for eixo, ax in ((g_fixa.x, 2), (g_fixa.y, 1), (g_fixa.z, 0))])
    cm = np.array([np.average(eixo, weights=corpo_m.sum(axis=tuple(i for i in range(3) if i != ax)))
                   for eixo, ax in ((g_movel.x, 2), (g_movel.y, 1), (g_movel.z, 0))])
    x = np.r_[0.0, 0.0, 0.0, cm - cf]                    # rotações (°) e translação (mm): começa nos centros de massa
    hist = []
    for nivel in niveis_mm:
        gg = grade_grossa(g_fixa, nivel)
        Fg = reamostrar(F, g_fixa, gg, fora=0.0)
        mg = reamostrar(corpo_f.astype(np.float32), g_fixa, gg, fora=0.0) > 0.5
        mg = ndimage.binary_dilation(mg, iterations=1)
        Ms = ndimage.gaussian_filter(M, [nivel / (2.5 * p) for p in g_movel.passo])
        p0 = pontos(gg)[mg.ravel()]
        f0 = Fg[mg]

        def custo(v):
            reg = Registro(tuple(v[:3]), tuple(v[3:]), tuple(cf))
            mv = amostrar(Ms, g_movel, reg.aplicar(p0), fora=0.0)
            pen = 1e-3 * float(np.sum(np.clip(np.abs(v[:3]) - max_rot_graus, 0, None) ** 2))
            return -ncc(f0, mv) + pen
        r = minimize(custo, x, method="Powell", options={"xtol": 1e-2, "ftol": 1e-5, "maxfev": 1500})
        x = r.x
        hist.append(round(-float(r.fun), 4))
        if progresso:
            progresso(f"Registro rígido: nível {nivel:g} mm, correlação {-r.fun:.3f}")
    return Registro(tuple(float(v) for v in x[:3]), tuple(float(v) for v in x[3:]), tuple(float(v) for v in cf),
                    metricas={"ncc_rigido_niveis": hist})


# ------------------------------------------------------------------------------------------------ deformável
def _gradiente(v: np.ndarray, passo) -> list[np.ndarray]:
    gz, gy, gx = np.gradient(v, *passo)
    return [gx, gy, gz]                                   # na ordem de u: (dx, dy, dz)


def jacobiano(u: np.ndarray, passo) -> np.ndarray:
    """det(I + ∇u) por voxel (u [3, nz, ny, nx] em mm; ordem dx, dy, dz)."""
    d = [[None] * 3 for _ in range(3)]
    for i in range(3):
        gz, gy, gx = np.gradient(u[i], *passo)
        d[i] = [gx, gy, gz]
    J = np.empty((3, 3) + u.shape[1:], np.float32)
    for i in range(3):
        for j in range(3):
            J[i, j] = d[i][j] + (1.0 if i == j else 0.0)
    return (J[0, 0] * (J[1, 1] * J[2, 2] - J[1, 2] * J[2, 1]) - J[0, 1] * (J[1, 0] * J[2, 2] - J[1, 2] * J[2, 0])
            + J[0, 2] * (J[1, 0] * J[2, 1] - J[1, 1] * J[2, 0]))


def deformavel(fixa: np.ndarray, g_fixa: Grade, movel: np.ndarray, g_movel: Grade, reg: Registro,
               mascara: np.ndarray | None = None, niveis_mm=(6.0, 3.0), iteracoes=(60, 40),
               sigma_campo_mm: float = 4.0, sigma_fluido_mm: float = 2.0, passo_max_mm: float = 2.0,
               progresso=None) -> Registro:
    """Demons multirresolução a partir do registro rígido `reg`. Devolve um novo Registro com o campo u."""
    t0 = time.time()
    F, M = _norm(fixa), _norm(movel)
    corpo = mascara if mascara is not None else fixa > -400
    u = None
    g_ant = None
    for nivel, n_it in zip(niveis_mm, iteracoes):
        gg = grade_grossa(g_fixa, nivel)
        Fg = reamostrar(ndimage.gaussian_filter(F, [nivel / (2.5 * p) for p in g_fixa.passo]), g_fixa, gg, fora=0.0)
        mg = reamostrar(corpo.astype(np.float32), g_fixa, gg, fora=0.0) > 0.5
        mg = ndimage.binary_dilation(mg, iterations=2)
        Ms = ndimage.gaussian_filter(M, [nivel / (2.5 * p) for p in g_movel.passo])
        p0 = pontos(gg)
        if u is None:
            u = np.zeros((3,) + gg.forma, np.float32)
        else:                                             # campo do nível anterior interpolado para a grade nova
            tmp = Registro(grade_def=g_ant, u=u)
            u = tmp.deslocamento(p0).T.reshape((3,) + gg.forma).astype(np.float32)
        passo = gg.passo
        gF = _gradiente(Fg, passo)
        sig_c = [sigma_campo_mm / p for p in passo]
        sig_f = [sigma_fluido_mm / p for p in passo]
        R = reg.matriz()
        c = np.asarray(reg.centro_mm)
        t = np.asarray(reg.translacao_mm)
        for it in range(n_it):
            q = (p0 + u.reshape(3, -1).T - c) @ R.T + c + t
            Mw = amostrar(Ms, g_movel, q, fora=0.0).reshape(gg.forma)
            dif = Mw - Fg
            gM = _gradiente(Mw, passo)                     # gradiente da móvel já deformada (força simétrica)
            gs = [0.5 * (a + b) for a, b in zip(gF, gM)]
            n2 = gs[0] ** 2 + gs[1] ** 2 + gs[2] ** 2
            den = n2 + (dif ** 2) / (passo_max_mm ** 2) + 1e-9
            du = np.stack([-dif * g / den for g in gs])
            du *= mg
            nrm = np.sqrt((du ** 2).sum(axis=0))
            du *= np.minimum(1.0, passo_max_mm / np.maximum(nrm, 1e-9))
            for k in range(3):
                du[k] = ndimage.gaussian_filter(du[k], sig_f)
                u[k] = ndimage.gaussian_filter(u[k] + du[k], sig_c)
            if progresso and it % 10 == 0:
                progresso(f"Registro deformável: nível {nivel:g} mm, iteração {it + 1}/{n_it}")
        g_ant = gg
    jac = jacobiano(u, g_ant.passo)
    mg_f = reamostrar(corpo.astype(np.float32), g_fixa, g_ant, fora=0.0) > 0.5
    nrm = np.sqrt((u ** 2).sum(axis=0))
    out = Registro(reg.rotacao_graus, reg.translacao_mm, reg.centro_mm, g_ant, u,
                   dict(reg.metricas))
    out.metricas.update({"desloc_max_mm": round(float(nrm[mg_f].max()) if mg_f.any() else 0.0, 1),
                         "desloc_medio_mm": round(float(nrm[mg_f].mean()) if mg_f.any() else 0.0, 1),
                         "jacobiano_min": round(float(jac[mg_f].min()) if mg_f.any() else 1.0, 3),
                         "jacobiano_max": round(float(jac[mg_f].max()) if mg_f.any() else 1.0, 3),
                         "dobras_pct": round(float((jac[mg_f] <= 0).mean() * 100) if mg_f.any() else 0.0, 3),
                         "tempo_s": round(time.time() - t0, 1)})
    return out


def qualidade(fixa: np.ndarray, g_fixa: Grade, movel: np.ndarray, g_movel: Grade, reg: Registro,
              mascara: np.ndarray | None = None, passo_mm: float = 4.0) -> dict:
    """Correlação e erro médio em HU no corpo (grade de passo_mm) para a transformação dada."""
    gg = grade_grossa(g_fixa, passo_mm)
    f = reamostrar(fixa, g_fixa, gg)
    m = reamostrar(movel, g_movel, gg, reg)
    corpo = reamostrar((mascara if mascara is not None else fixa > -400).astype(np.float32), g_fixa, gg, fora=0) > 0.5
    return {"ncc": round(ncc(_norm(f), _norm(m), corpo), 4), "mae_hu": round(float(np.abs(f - m)[corpo].mean()), 1)}


def propagar_mascara(masc_movel: np.ndarray, g_movel: Grade, destino: Grade, reg: Registro) -> np.ndarray:
    """Máscara da secundária levada para a grade da principal (amostra da máscara em T(x), limiar 0,5)."""
    return reamostrar(masc_movel.astype(np.float32), g_movel, destino, reg, ordem=1, fora=0.0) >= 0.5
