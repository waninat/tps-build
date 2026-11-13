"""Compensador de fluência (equivalente ao "electronic compensator" do Eclipse). SRS-PLN-060..066.

Para cada campo escolhido, uma fluência F(u, v) na grade do MLC (linhas = pares de lâminas, colunas de 2,5 mm) é
corrigida iterativamente para que a dose média ao longo de cada raio, dentro do alvo, chegue à prescrição:

    F ← F · (D_prescrita / D_raio)^γ          (γ = 0,8; F limitada a [0,5; 1,5]; suavização leve em u)

A fluência é então sequenciada em níveis (step-and-shoot): o nível j abre, em cada par, o maior trecho contíguo em
que F ≥ j·Δ. Cada segmento passa pela mesma mecânica do ajuste 3D (gap mínimo, pares fechados, não-interdigitação
do MLCi) e pela verificação mecânica. Segmentos com menos de `um_min_segmento` UM são fundidos ao nível vizinho.

Diferente do Eclipse, não precisa de "pintura" manual da fluência nem de plano de compensação escolhido à mão: a
referência é a dose no próprio alvo, com todos os campos somados. A pintura manual fica para a interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import gaussian_filter1d, map_coordinates

from tps.engine.geometry import beam_frame
from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import OpcoesAjuste, _posicionar_fechados, verificar_mecanica
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano


@dataclass
class ResultadoCompensador:
    fluencias: dict                      # nome → (u, F[n_pares, n_u])
    n_segmentos: dict
    hi_antes: float
    hi_depois: float
    historico: list = field(default_factory=list)
    avisos: list = field(default_factory=list)


def _hi(d, alvo):
    v = d[alvo]
    return (np.percentile(v, 98) - np.percentile(v, 2)) / np.percentile(v, 50)


def _grade_u(c: Campo, colim: Colimacao, res: float):
    jx = c.jaw_x if c.jaw_x else (min(c.mlc_a), max(c.mlc_b))
    u = np.arange(np.floor(jx[0] / res) * res + res / 2, jx[1], res)
    lim = colim.limites_laminas
    v = 0.5 * (lim[1:] + lim[:-1])
    aberto = np.zeros((colim.n_pares, len(u)), bool)
    for i in range(colim.n_pares):
        if lim[i + 1] > c.jaw_y[0] and lim[i] < c.jaw_y[1] and c.mlc_b[i] - c.mlc_a[i] > colim.gap_minimo_mm + 0.5:
            aberto[i] = (u > c.mlc_a[i]) & (u < c.mlc_b[i])
    return u, v, aberto


def _dose_no_raio(dose, alvo, grade, c: Campo, u, v, posicao):
    """Dose média ao longo de cada raio (u, v) dentro do alvo; NaN onde o raio não cruza o alvo."""
    fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, posicao)
    kz, ky, kx = np.nonzero(alvo)
    pts = np.column_stack([grade.x[kx], grade.y[ky], grade.z[kz]])
    dist = np.linalg.norm(pts - fr.source, axis=1)
    passo = abs(grade.x[1] - grade.x[0]) / 2
    ts = np.arange(dist.min() - passo, dist.max() + passo, passo)
    iso_plano = fr.source + 1000.0 * fr.w
    U, V = np.meshgrid(u, v)
    P = iso_plano + U[..., None] * fr.u + V[..., None] * fr.v
    d = P - fr.source
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    amostras = fr.source + ts[None, None, :, None] * d[:, :, None, :]           # [nv, nu, nt, 3]
    ix = (amostras[..., 0] - grade.x[0]) / (grade.x[1] - grade.x[0])
    iy = (amostras[..., 1] - grade.y[0]) / (grade.y[1] - grade.y[0])
    iz = (amostras[..., 2] - grade.z[0]) / (grade.z[1] - grade.z[0])
    coords = [iz.ravel(), iy.ravel(), ix.ravel()]
    dentro = map_coordinates(alvo.astype(np.float32), coords, order=0, mode="constant").reshape(ix.shape) > 0.5
    dd = map_coordinates(dose, coords, order=1, mode="constant").reshape(ix.shape)
    n = dentro.sum(axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, (dd * dentro).sum(axis=-1) / np.maximum(n, 1), np.nan)


def sequenciar(c: Campo, colim: Colimacao, u: np.ndarray, F: np.ndarray, aberto: np.ndarray,
               niveis: int) -> list[dict]:
    """Fluência → segmentos: um segmento-base com o mínimo da fluência (abertura inteira) + `niveis` níveis iguais
    da parte que excede o mínimo (maior trecho contíguo por par). Assim a resolução dos níveis fica toda na faixa
    útil da compensação, e não gasta níveis de 0 até o mínimo."""
    res = u[1] - u[0]
    Fm = np.where(aberto, F, 0.0)
    if Fm.max() <= 0:
        raise ValueError(f"{c.nome}: fluência nula")
    base = float(np.percentile(Fm[aberto], 1))
    resid = np.where(aberto, np.maximum(Fm - base, 0.0), -1.0)
    delta = resid.max() / niveis if resid.max() > 1e-6 else 0.0
    camadas = [(np.where(aberto, 1.0, 0.0), 0.5, base)]           # (mapa, limiar, peso)
    camadas += [(resid, (j - 0.5) * delta, delta) for j in range(1, niveis + 1)] if delta > 0 else []
    segs = []
    op = OpcoesAjuste()
    for mapa, lim_j, peso in camadas:
        if peso <= 0:
            continue
        a = np.full(colim.n_pares, np.nan)
        b = np.full(colim.n_pares, np.nan)
        for i in range(colim.n_pares):
            cols = np.nonzero(mapa[i] >= lim_j)[0]
            if len(cols) == 0:
                continue
            quebras = np.nonzero(np.diff(cols) > 1)[0]
            if len(quebras):
                cols = max(np.split(cols, quebras + 1), key=lambda t: float(mapa[i, t].sum()))
            a[i] = max(u[cols[0]] - res / 2, c.mlc_a[i])
            b[i] = min(u[cols[-1]] + res / 2, c.mlc_b[i])
        if np.all(np.isnan(a)):
            continue
        jy = c.jaw_y
        jx = c.jaw_x
        _posicionar_fechados(a, b, colim, jx, op)
        s = Campo(c.nome, c.gantry, c.colimador, c.mesa, c.isocentro_mm, jaw_x=jx, jaw_y=jy,
                  mlc_a=[round(float(x), 1) for x in a], mlc_b=[round(float(x), 1) for x in b])
        erros = verificar_mecanica(s, colim)
        if erros:
            raise ValueError("; ".join(erros))
        segs.append({"mlc_a": s.mlc_a, "mlc_b": s.mlc_b, "jaw_x": jx, "jaw_y": jy, "peso": peso})
    tot = sum(s["peso"] for s in segs)
    for s in segs:
        s["peso"] /= tot
    return segs


def _fundir_pequenos(segs: list[dict], mu_campo: float, um_min: float) -> list[dict]:
    """Funde segmentos com UM < mínimo ao segmento seguinte (ou anterior)."""
    segs = [dict(s) for s in segs]
    mudou = True
    while mudou and len(segs) > 1:
        mudou = False
        for k, s in enumerate(segs):
            if s["peso"] * mu_campo < um_min:
                viz = k + 1 if k + 1 < len(segs) else k - 1
                segs[viz]["peso"] += s["peso"]
                segs.pop(k)
                mudou = True
                break
    return segs


def compensar(plano: Plano, grade: calculo.GradeDose, doses_um: dict, alvo: np.ndarray, colim: Colimacao, modelo,
              campos: list[str] | None = None, niveis: int = 6, iteracoes: int = 4, res_u_mm: float = 2.5,
              gama: float = 0.6, limites=(0.5, 1.5), um_min_segmento: float = 2.0, kernels=None,
              posicao: str = "HFS", permitir_demonstracao: bool = False) -> ResultadoCompensador:
    """Aplica o compensador aos campos (padrão: todos os principais). Altera o plano e `doses_um`."""
    plano._exigir_rascunho()
    alvos = [c for c in plano.campos if c.campo_pai is None and (campos is None or c.nome in campos)]
    if not alvos:
        raise ValueError("Nenhum campo para compensar")
    rx = plano.prescricao.dose_fracao_gy
    dose = calculo.normalizar(plano, doses_um, grade, alvo)
    hi0 = _hi(dose, alvo)
    hist = [hi0]
    mapas = {}
    for c in alvos:
        u, v, ab = _grade_u(c, colim, res_u_mm)
        mapas[c.nome] = (u, v, ab, np.where(ab, 1.0, 0.0))
    avisos = []
    melhor = (hi0, None, None)
    for _ in range(iteracoes):
        for c in alvos:
            u, v, ab, F = mapas[c.nome]
            R = _dose_no_raio(dose, alvo, grade, c, u, v, posicao)
            corr = np.where(np.isfinite(R) & ab, (rx / np.where(np.isfinite(R) & (R > 0), R, rx)) ** gama, 1.0)
            F = np.clip(F * corr, limites[0], limites[1]) * ab
            F = np.where(ab, gaussian_filter1d(F, 1.0, axis=1), 0.0)
            media = F[ab & np.isfinite(R)].mean() if (ab & np.isfinite(R)).any() else 1.0
            F = np.where(ab, F / media, 0.0)
            mapas[c.nome] = (u, v, ab, F)
            c.segmentos = sequenciar(c, colim, u, F, ab, niveis)
        doses_um.update(calculo.dose_por_um(grade, alvos, colim, modelo, kernels, posicao, permitir_demonstracao))
        dose = calculo.normalizar(plano, doses_um, grade, alvo)
        hist.append(_hi(dose, alvo))
        if hist[-1] < melhor[0]:
            melhor = (hist[-1], {c.nome: [dict(x) for x in c.segmentos] for c in alvos},
                      {c.nome: doses_um[c.nome] for c in alvos})
    if melhor[1] is None:                              # nada a compensar: volta aos campos abertos
        for c in alvos:
            c.segmentos = None
        doses_um.update(calculo.dose_por_um(grade, alvos, colim, modelo, kernels, posicao, permitir_demonstracao))
        calculo.normalizar(plano, doses_um, grade, alvo)
        return ResultadoCompensador({}, {c.nome: 0 for c in alvos}, hi0, hi0, hist,
                                    ["Compensador sem ganho (a heterogeneidade está ao longo do eixo dos feixes, que a "
                                     "fluência não corrige): campos mantidos abertos"])
    for c in alvos:                                    # fica a melhor iteração (a correção pode oscilar)
        c.segmentos = melhor[1][c.nome]
        doses_um[c.nome] = melhor[2][c.nome]
    dose = calculo.normalizar(plano, doses_um, grade, alvo)
    # UM mínima por segmento
    for c in alvos:
        novos = _fundir_pequenos(c.segmentos, c.mu, um_min_segmento)
        if len(novos) != len(c.segmentos):
            avisos.append(f"{c.nome}: {len(c.segmentos) - len(novos)} segmento(s) fundido(s) (< {um_min_segmento} UM)")
            c.segmentos = novos
            doses_um.update(calculo.dose_por_um(grade, [c], colim, modelo, kernels, posicao, permitir_demonstracao))
    dose = calculo.normalizar(plano, doses_um, grade, alvo)
    for c in alvos:                                    # o envelope do campo vira a união dos segmentos
        c.mlc_a = [min(s["mlc_a"][i] for s in c.segmentos) for i in range(colim.n_pares)]
        c.mlc_b = [max(s["mlc_b"][i] for s in c.segmentos) for i in range(colim.n_pares)]
    return ResultadoCompensador({n: (m[0], m[3]) for n, m in mapas.items()},
                                {c.nome: len(c.segmentos) for c in alvos}, hi0, _hi(dose, alvo), hist, avisos)
