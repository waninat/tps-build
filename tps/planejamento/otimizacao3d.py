"""Otimizações do planejamento 3D direto (forward). SRS-PLN-050..058.

- `otimizar_pesos`: pesos relativos dos campos que tornam a dose no alvo a mais homogênea possível (mínimos quadrados
  com pesos ≥ mínimo), com penalidade opcional de dose média em órgãos de risco. O Eclipse e o Monaco deixam os pesos
  na mão do planejador.
- `field_in_field`: sub-campos automáticos que bloqueiam, na visão de cada feixe, as regiões quentes (> nível × dose
  prescrita), com pesos otimizados e UM mínima por sub-campo. Substitui a cunha (o serviço não usa cunha) e faz em
  segundos o que no Eclipse/Monaco é manual (desenhar o sub-campo, copiar o campo, ajustar peso).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import lsq_linear

from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import OpcoesAjuste, ajustar, grade_bev, raster_da_abertura
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano


def _amostra(mask: np.ndarray, n_max: int, rng) -> np.ndarray:
    idx = np.flatnonzero(mask)
    if len(idx) > n_max:
        idx = rng.choice(idx, n_max, replace=False)
    return idx


def otimizar_pesos(doses_um: dict[str, np.ndarray], alvo: np.ndarray, oars: list[tuple] | None = None,
                   peso_min_rel: float = 0.05, n_max: int = 20000, semente: int = 0) -> dict[str, float]:
    """Pesos (soma = nº de campos) que minimizam a variância relativa da dose no alvo.

    oars: [(máscara, importância)] — acrescenta a dose média do órgão (relativa à do alvo) × importância ao custo.
    """
    rng = np.random.default_rng(semente)
    nomes = list(doses_um)
    idx = _amostra(alvo, n_max, rng)
    if len(idx) == 0:
        raise ValueError("Alvo vazio")
    A = np.column_stack([doses_um[n].ravel()[idx] for n in nomes])
    escala = A.mean()
    A = A / escala
    b = np.ones(len(idx))
    linhas, alvos = [A / np.sqrt(len(idx))], [b / np.sqrt(len(idx))]
    for masc, imp in oars or []:
        if masc.any():
            media = np.array([doses_um[n][masc].mean() for n in nomes]) / escala
            linhas.append(np.sqrt(imp) * media[None, :])
            alvos.append(np.zeros(1))
    M, y = np.vstack(linhas), np.concatenate(alvos)
    r = lsq_linear(M, y, bounds=(0, np.inf))
    w = r.x
    if w.max() <= 0:
        raise ValueError("Otimização de pesos sem solução")
    piso = peso_min_rel * w.max()
    if (w < piso).any():                               # nenhum campo some: reimpõe o mínimo e re-otimiza
        r = lsq_linear(M, y, bounds=(np.full(len(nomes), piso), np.inf))
        w = r.x
    w = w / w.sum() * len(nomes)
    return {n: float(v) for n, v in zip(nomes, w)}


@dataclass
class ResultadoFiF:
    subcampos: list[Campo]
    dmax_antes_pct: float
    dmax_depois_pct: float
    v_quente_antes_cc: float
    v_quente_depois_cc: float
    hi_antes: float
    hi_depois: float
    avisos: list[str] = field(default_factory=list)


def indice_homogeneidade(d: np.ndarray, alvo: np.ndarray) -> float:
    """HI = (D2 − D98) / D50 (ICRU 83)."""
    v = np.sort(d[alvo])
    if len(v) == 0:
        return float("nan")
    p = lambda q: float(np.percentile(v, q))          # noqa: E731
    return (p(98) - p(2)) / p(50)


def field_in_field(plano: Plano, grade: calculo.GradeDose, doses_um: dict[str, np.ndarray], alvo: np.ndarray,
                   corpo: np.ndarray, colim: Colimacao, modelo, niveis=(1.07, 1.05, 1.03), um_min: float = 3.0,
                   campos_base: list[str] | None = None, vol_min_cc: float = 0.3, area_min_cm2: float = 4.0,
                   kernels=None, posicao: str = "HFS", permitir_demonstracao: bool = False,
                   excluir_superficie_mm: float = 5.0) -> ResultadoFiF:
    """Acrescenta sub-campos ao plano (em rascunho) e recalcula as UM. Altera `plano` e `doses_um`.

    campos_base: campos que recebem sub-campos (padrão: todos os campos principais).
    Regiões quentes a menos de `excluir_superficie_mm` da pele não disparam sub-campo (build-up).
    """
    plano._exigir_rascunho()
    eixos = (grade.x, grade.y, grade.z)
    principais = [c for c in plano.campos if c.campo_pai is None]
    base = [c for c in principais if campos_base is None or c.nome in campos_base]
    from scipy.ndimage import binary_erosion
    passo = abs(grade.x[1] - grade.x[0])
    interior = binary_erosion(corpo, iterations=max(1, int(round(excluir_superficie_mm / passo))))
    d0 = calculo.normalizar(plano, doses_um, grade, alvo)
    rx = plano.prescricao.dose_fracao_gy
    antes = (float(d0[corpo].max() / rx * 100), float(((d0 > niveis[-1] * rx) & interior).sum() * grade.voxel_cc),
             indice_homogeneidade(d0, alvo))
    avisos, novos = [], []
    dose = d0
    pesos_ref = {c.nome: c.peso for c in principais}
    for k, nivel in enumerate(niveis, start=1):
        quente = (dose > nivel * rx) & interior
        if quente.sum() * grade.voxel_cc < vol_min_cc:
            continue
        criados = []
        for c in base:
            sub = copy.deepcopy(c)
            sub.nome = f"{c.nome[:12]}s{k}"
            sub.mu, sub.campo_pai, sub.peso = None, c.nome, 0.0
            u, v = grade_bev(colim.jaw_y_max_mm + 5, 1.0)
            pai = raster_da_abertura(c, colim, u, v)
            try:
                r = ajustar(sub, colim, alvos=[], blindagens=[], eixos=eixos, op=OpcoesAjuste("area", segmentos="maior"),
                            posicao=posicao, abertura_base=_menos(pai, quente, eixos, sub, posicao))
            except ValueError as e:
                avisos.append(f"{sub.nome}: sem sub-campo ({e})")
                continue
            if r.abertura.area_cm2() < area_min_cm2:
                continue
            criados.append(sub)
        if not criados:
            continue
        doses_um.update(calculo.dose_por_um(grade, criados, colim, modelo, kernels, posicao, permitir_demonstracao))
        for s in criados:
            plano.adicionar_campo(s)
        novos += criados
        dose = _reotimizar(plano, doses_um, grade, alvo, corpo & interior, rx, um_min, avisos, pesos_ref)
    if novos:
        dose = _reotimizar(plano, doses_um, grade, alvo, corpo & interior, rx, um_min, avisos, pesos_ref)
    novos = [c for c in plano.campos if c in novos]
    return ResultadoFiF(novos, antes[0], float(dose[corpo].max() / rx * 100), antes[1],
                        float(((dose > niveis[-1] * rx) & interior).sum() * grade.voxel_cc), antes[2],
                        indice_homogeneidade(dose, alvo), avisos)


def _menos(pai, quente, eixos, campo, posicao):
    from tps.planejamento.ajuste_mlc import RasterBEV, bev_da_estrutura
    q = bev_da_estrutura(quente, eixos, campo, pai.u, pai.v, posicao=posicao)
    return RasterBEV(pai.u, pai.v, pai.m & ~q.m)


def _reotimizar(plano, doses_um, grade, alvo, regiao, rx, um_min, avisos, pesos_ref: dict,
                frac_principal: float = 0.6):
    """Pesos de todos os campos: alvo homogêneo e nada acima da prescrição fora do alvo (mínimos quadrados).

    Cada campo principal mantém pelo menos `frac_principal` do peso que teria sozinho (o field-in-field refina a
    distribuição; não troca o campo aberto por sub-campos)."""
    for _ in range(5):
        nomes = [c.nome for c in plano.campos]
        principal = np.array([c.campo_pai is None for c in plano.campos])
        rng = np.random.default_rng(0)
        ia = _amostra(alvo, 15000, rng)
        soma = sum(doses_um[n] for n in nomes)
        iq = _amostra(regiao & ~alvo & (soma > np.percentile(soma[alvo], 50)), 5000, rng)
        A = np.column_stack([doses_um[n].ravel()[np.r_[ia, iq]] for n in nomes])
        A /= A.mean()
        y = np.ones(A.shape[0])
        w_ref = np.array([pesos_ref.get(c.nome, 0.0) for c in plano.campos])   # pesos dos principais antes do FiF
        esc = float(np.dot(A @ w_ref, y) / np.dot(A @ w_ref, A @ w_ref))   # escala dos principais sozinhos
        lb = np.where(principal, frac_principal * esc * w_ref, 0.0)
        r = lsq_linear(A, y, bounds=(lb, np.inf))
        # mínimos quadrados reponderados: pontos quentes (> 103%) pesam mais, para não trocar homogeneidade média
        # por picos locais (a largura de 1 cm do MLCi cria degraus nos sub-campos)
        pesos_l = np.ones(A.shape[0])
        for _ in range(3):
            rel = A @ r.x
            rel = rel / np.mean(rel[:len(ia)])
            pesos_l = np.where(rel > 1.03, pesos_l * 2.0, pesos_l)
            r = lsq_linear(A * pesos_l[:, None], y * pesos_l, bounds=(lb, np.inf))
        for c, w in zip(plano.campos, r.x):
            c.peso = float(max(w, 1e-6))
        dose = calculo.normalizar_sem_minimo(plano, doses_um, grade, alvo)
        fracos = [c for c in plano.campos if c.mu < um_min and c.campo_pai is not None]
        if not fracos:
            break
        for c in fracos:
            avisos.append(f"{c.nome}: removido ({c.mu:.1f} UM < {um_min} UM)")
            plano.campos.remove(c)
            doses_um.pop(c.nome, None)
    return calculo.normalizar(plano, doses_um, grade, alvo)
