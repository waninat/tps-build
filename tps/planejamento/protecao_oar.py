"""Otimização direta 3D com objetivos por órgão de risco (OAR) e ajuste fino de pesos. SRS-PLN-070..076.

No 3D conformado o "otimizador" só tem dois botões: o peso de cada campo e a forma de sub-campos. Aqui:

`proteger_oars`
  1. para cada campo principal e cada OAR que ele atravessa, cria um sub-campo = campo − projeção BEV do OAR
     (com margem), sem fechar a parte do OAR que está dentro do alvo (o alvo tem prioridade);
  2. otimiza os pesos de todos os campos e sub-campos (L-BFGS-B, gradiente analítico) minimizando
        Σ_alvo (D/Rx − 1)²  +  5·Σ_alvo (0,95 − D/Rx)²₊  +  Σ_corpo (D/Rx − 1,05)²₊
        + Σ_OAR prioridade·[objetivo do órgão]
     onde o objetivo é: Dmean ≤ L  → (média/L − 1)²₊ ;  Dmax/D…cc ≤ L → média dos (D/L − 1)²₊ ;
     V(x Gy) ≤ y% → os voxels acima de x Gy além da fração y são empurrados para baixo de x;
     sem limite ("o menor possível") → (média/Rx)²;
  3. cada campo principal guarda um peso mínimo; sub-campos abaixo da UM mínima saem; normaliza pela prescrição.

`ajuste_fino`
  Re-otimiza os pesos que já existem (ex.: depois do field-in-field "filtro") para homogeneidade e gradiente:
  mesmo custo do alvo + penalidade de dose no anel de 0–2 cm em volta do alvo acima de 90% (queda mais rápida),
  com regularização para não se afastar muito dos pesos de partida.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import binary_dilation, binary_erosion
from scipy.optimize import minimize

from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import (Margem, OpcoesAjuste, RasterBEV, ajustar, bev_da_estrutura, dilatar,
                                        grade_bev, raster_da_abertura)
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Plano
from tps.planejamento.otimizacao3d import indice_homogeneidade


@dataclass
class ObjetivoOAR:
    nome: str
    mascara: np.ndarray
    metrica: str = "Dmean"          # Dmean | Dmax | V   (V: volume acima de `dose_gy` ≤ `volume_pct`)
    valor_gy: float | None = None   # limite (Gy, dose total); None = o menor possível
    volume_pct: float | None = None
    prioridade: float = 5.0


@dataclass
class ResultadoOAR:
    subcampos: list
    hi_antes: float
    hi_depois: float
    oars: dict = field(default_factory=dict)     # nome → {"antes": {...}, "depois": {...}}
    avisos: list = field(default_factory=list)


def _metricas(total, m, vox):
    v = total[m]
    if v.size == 0:
        return {}
    return {"Dmean": round(float(v.mean()), 2), "Dmax": round(float(v.max()), 2),
            "D2cc": round(float(np.sort(v)[::-1][min(v.size, max(1, int(np.ceil(2 / vox)))) - 1]), 2)}


def _otimizar(A_alvo, oars_amostras, A_quente, A_anel, w0, lb, rx_fx, frac, reg=0.0):
    """Minimiza o custo em w ≥ lb. A_*: [n_voxels, n_campos] em Gy/UM (por fração)."""
    escala = 1.0 / rx_fx
    w_ref = w0.copy()

    def custo(w):
        f, g = 0.0, np.zeros_like(w)
        d = A_alvo @ w * escala
        r = d - 1.0
        # o alvo manda: homogeneidade e, sobretudo, nada abaixo de 97% (cobertura não se troca por órgão)
        f += 10 * np.mean(r ** 2)
        g += 20 * A_alvo.T @ r * escala / len(d)
        sub = np.clip(0.97 - d, 0, None)
        f += 300 * np.mean(sub ** 2)
        g -= 600 * A_alvo.T @ sub * escala / len(d)
        if A_quente is not None and len(A_quente):
            dq = A_quente @ w * escala
            e = np.clip(dq - 1.05, 0, None)
            f += np.mean(e ** 2) * 2
            g += 4 * A_quente.T @ e * escala / len(dq)
        if A_anel is not None and len(A_anel):
            da = A_anel @ w * escala
            e = np.clip(da - 0.90, 0, None)
            f += np.mean(e ** 2)
            g += 2 * A_anel.T @ e * escala / len(da)
        for o, A in oars_amostras:
            dd = A @ w * frac                                   # dose total (Gy)
            p = o.prioridade / 10.0
            if o.valor_gy is None:                              # o menor possível
                mm = dd.mean() / (rx_fx * frac)
                f += p * mm ** 2
                g += p * 2 * mm * A.mean(axis=0) * frac / (rx_fx * frac)
            elif o.metrica == "Dmean":
                e = max(dd.mean() / o.valor_gy - 1.0, 0.0)
                f += 4 * p * e ** 2
                g += 8 * p * e * A.mean(axis=0) * frac / o.valor_gy
            elif o.metrica == "V" and o.volume_pct is not None:
                # só os voxels mais quentes que excedem a fração permitida
                k = int(np.floor(len(dd) * o.volume_pct / 100.0))
                ordem = np.argsort(dd)[::-1]
                sel = ordem[k:]
                e = np.clip(dd[sel] / o.valor_gy - 1.0, 0, None)
                f += 4 * p * np.mean(e ** 2) if len(sel) else 0.0
                if len(sel):
                    g += 8 * p * A[sel].T @ e * frac / o.valor_gy / len(sel)
            else:                                               # Dmax / D…cc
                e = np.clip(dd / o.valor_gy - 1.0, 0, None)
                f += 4 * p * np.mean(e ** 2) * 10
                g += 80 * p * A.T @ e * frac / o.valor_gy / len(dd)
        if reg > 0:
            q = (w - w_ref) / max(w_ref.sum(), 1e-9) * len(w)
            f += reg * np.sum(q ** 2)
            g += 2 * reg * q * len(w) / max(w_ref.sum(), 1e-9)
        return f, g

    r = minimize(custo, np.maximum(w0, lb), jac=True, method="L-BFGS-B",
                 bounds=[(float(a), None) for a in lb], options={"maxiter": 500})
    return r.x


def _amostra(mask, n, rng):
    idx = np.flatnonzero(mask)
    return rng.choice(idx, n, replace=False) if len(idx) > n else idx


def _matriz(doses_um, nomes, idx):
    return np.column_stack([doses_um[n].ravel()[idx] for n in nomes]).astype(np.float64)


def proteger_oars(plano: Plano, grade: calculo.GradeDose, doses_um: dict, alvo: np.ndarray, corpo: np.ndarray,
                  colim: Colimacao, modelo, objetivos: list[ObjetivoOAR], criar_subcampos: bool = True,
                  margem_mm: float = 5.0, um_min: float = 3.0, area_min_cm2: float = 6.0, frac_principal: float = 0.2,
                  kernels=None, posicao: str = "HFS", permitir_demonstracao: bool = False,
                  tol_hi: float = 0.03, tol_d98: float = 0.01) -> ResultadoOAR:
    plano._exigir_rascunho()
    if not objetivos:
        raise ValueError("Escolha pelo menos um órgão para proteger")
    eixos = (grade.x, grade.y, grade.z)
    rx = plano.prescricao.dose_fracao_gy
    frac = plano.prescricao.fracoes
    d0 = calculo.normalizar(plano, doses_um, grade, alvo)
    hi0 = indice_homogeneidade(d0, alvo)
    antes = {o.nome: _metricas(d0 * frac, o.mascara, grade.voxel_cc) for o in objetivos}
    principais = [c for c in plano.campos if c.campo_pai is None]
    avisos, novos = [], []
    if criar_subcampos:
        u, v = grade_bev(colim.jaw_y_max_mm + 5, 1.0)
        for c in principais:
            pai = raster_da_abertura(c, colim, u, v)
            pt = bev_da_estrutura(alvo, eixos, c, u, v, posicao=posicao)
            for k, o in enumerate(objetivos, start=1):
                if not o.mascara.any():
                    continue
                po = dilatar(bev_da_estrutura(o.mascara, eixos, c, u, v, posicao=posicao), Margem.uniforme(margem_mm))
                # dois sub-campos candidatos: (a) bloqueia o órgão só fora da projeção do alvo (não custa cobertura);
                # (b) bloqueia o órgão inteiro no BEV, inclusive onde o raio também cruza o alvo — os outros campos
                # repõem o alvo e a otimização dos pesos decide quanto usar (a cobertura tem prioridade no custo).
                # Na pelve o reto/bexiga ficam quase todos atrás/na frente do PTV no BEV: sem (b) não há o que bloquear.
                for sufixo, bloqueio in (("o", po.m & pai.m & ~pt.m), ("b", po.m & pai.m)):
                    if bloqueio.sum() * 1.0 / 100.0 < 1.0:          # < 1 cm² do órgão no campo
                        continue
                    if sufixo == "b" and (po.m & pai.m & pt.m).sum() < 0.3 * bloqueio.sum():
                        continue                                    # quase igual ao (a)
                    sub = copy.deepcopy(c)
                    sub.nome = f"{c.nome[:10]}{sufixo}{k}"
                    sub.mu, sub.campo_pai, sub.peso = None, c.nome, 0.0
                    try:
                        ajustar(sub, colim, alvos=[], blindagens=[], eixos=eixos,
                                op=OpcoesAjuste("area", segmentos="maior"), posicao=posicao,
                                abertura_base=RasterBEV(u, v, pai.m & ~bloqueio))
                    except ValueError as e:
                        avisos.append(f"{sub.nome}: sem sub-campo ({e})")
                        continue
                    ab = raster_da_abertura(sub, colim, u, v)
                    if ab.area_cm2() < area_min_cm2 or ab.m.sum() >= pai.m.sum() * 0.97:
                        continue
                    sub.descricao = (f"protege {o.nome}" + (" (inclui sobre o alvo)" if sufixo == "b" else ""))[:64]
                    novos.append(sub)
        if novos:
            doses_um.update(calculo.dose_por_um(grade, novos, colim, modelo, kernels, posicao, permitir_demonstracao))
            for s in novos:
                plano.adicionar_campo(s)
    interior = binary_erosion(corpo, iterations=2)
    mu_inicial = {c.nome: float(c.mu or 0.0) for c in principais}   # o piso do campo principal não encolhe a cada volta
    d98_0 = float(np.percentile(d0[alvo], 2)) / rx
    campos0, doses0 = list(plano.campos), dict(doses_um)
    pesos0 = {c.nome: (c.peso, c.mu) for c in plano.campos}
    prio0 = [o.prioridade for o in objetivos]

    def rodada(escala):
        """Otimiza os pesos com as prioridades dos órgãos × escala; devolve (HI, D98/Rx, campos, doses, pesos)."""
        rng = np.random.default_rng(0)
        plano.campos[:] = list(campos0)
        doses_um.clear()
        doses_um.update(doses0)
        for c in plano.campos:
            c.peso, c.mu = pesos0[c.nome]
        for o, p0 in zip(objetivos, prio0):
            o.prioridade = p0 * escala
        for _ in range(4):
            nomes = [c.nome for c in plano.campos]
            principal = np.array([c.campo_pai is None for c in plano.campos])
            soma = sum(doses_um[n] for n in nomes)
            A_alvo = _matriz(doses_um, nomes, _amostra(alvo, 12000, rng))
            quente = interior & ~alvo & (soma > np.percentile(soma[alvo], 60))
            A_q = _matriz(doses_um, nomes, _amostra(quente, 6000, rng))
            oa = [(o, _matriz(doses_um, nomes, _amostra(o.mascara & ~alvo, 5000, rng))) for o in objetivos
                  if (o.mascara & ~alvo).any()]
            w0 = np.array([c.mu if c.campo_pai is None else 0.0 for c in plano.campos], float)
            lb = np.array([frac_principal * mu_inicial.get(c.nome, 0.0) if c.campo_pai is None else 0.0
                           for c in plano.campos])
            w0 = np.where(principal, np.maximum(w0, lb), 0.05 * w0.max())
            w = _otimizar(A_alvo, oa, A_q, None, w0, lb, rx, frac)
            for c, x in zip(plano.campos, w):
                c.peso = float(max(x, 1e-4))
            calculo.normalizar_sem_minimo(plano, doses_um, grade, alvo)
            fracos = [c for c in plano.campos if c.campo_pai is not None and (c.mu or 0) < um_min]
            if not fracos:
                break
            for c in fracos:
                plano.campos.remove(c)
                doses_um.pop(c.nome, None)
            for c in plano.campos:                          # recomeça dos pesos atuais
                if c.campo_pai is None:
                    c.peso = c.mu
        d = calculo.normalizar(plano, doses_um, grade, alvo)
        return (indice_homogeneidade(d, alvo), float(np.percentile(d[alvo], 2)) / rx, list(plano.campos),
                dict(doses_um), {c.nome: (c.peso, c.mu) for c in plano.campos}, d)

    # a cobertura do alvo não se troca por órgão: se a homogeneidade ou o D98 pioram além da tolerância, as
    # prioridades dos órgãos caem (½, ¼, 1/10) até o plano voltar à tolerância; fica a 1ª rodada aceitável
    tentativas = []
    for escala in (1.0, 0.5, 0.25, 0.1):
        r = rodada(escala)
        tentativas.append((escala, r))
        if r[0] <= hi0 + tol_hi and r[1] >= min(d98_0, 0.95) - tol_d98:
            break
    else:
        escala, r = min(tentativas, key=lambda t: t[1][0])
    for o, p0 in zip(objetivos, prio0):
        o.prioridade = p0
    hi1, d98_1, campos_r, doses_r, pesos_r, d = r
    plano.campos[:] = campos_r
    doses_um.clear()
    doses_um.update(doses_r)
    for c in plano.campos:
        c.peso, c.mu = pesos_r[c.nome]
    d = calculo.normalizar(plano, doses_um, grade, alvo)
    hi1 = indice_homogeneidade(d, alvo)
    oars = {o.nome: {"antes": antes[o.nome], "depois": _metricas(d * frac, o.mascara, grade.voxel_cc)}
            for o in objetivos}
    if escala < 1.0:
        avisos.append(f"Prioridade dos órgãos reduzida a {escala:g}× para manter a cobertura do alvo "
                      f"(D98 {100 * d98_0:.1f}% → {100 * d98_1:.1f}% da prescrição)")
    if hi1 > hi0 + tol_hi:
        avisos.append(f"A proteção dos órgãos piorou a homogeneidade do alvo (HI {hi0:.3f} → {hi1:.3f}); "
                      "baixe a prioridade ou afrouxe o objetivo")
    return ResultadoOAR([c for c in plano.campos if c in novos], hi0, hi1, oars, avisos)


def ajuste_fino(plano: Plano, grade: calculo.GradeDose, doses_um: dict, alvo: np.ndarray, corpo: np.ndarray,
                frac_principal: float = 0.5, um_min: float = 2.0, regularizacao: float = 0.05,
                anel_mm: float = 20.0) -> tuple[float, float, list]:
    """Re-otimiza os pesos existentes (campos e sub-campos) para homogeneidade e gradiente. Devolve (HI antes,
    HI depois, avisos)."""
    plano._exigir_rascunho()
    rx = plano.prescricao.dose_fracao_gy
    d0 = calculo.normalizar(plano, doses_um, grade, alvo)
    hi0 = indice_homogeneidade(d0, alvo)
    passo = abs(grade.x[1] - grade.x[0])
    anel = binary_dilation(alvo, iterations=max(1, int(round(anel_mm / passo)))) & ~alvo & corpo
    interior = binary_erosion(corpo, iterations=2)
    rng = np.random.default_rng(1)
    nomes = [c.nome for c in plano.campos]
    soma = sum(doses_um[n] for n in nomes)
    A_alvo = _matriz(doses_um, nomes, _amostra(alvo, 12000, rng))
    A_q = _matriz(doses_um, nomes, _amostra(interior & ~alvo & (soma > np.percentile(soma[alvo], 60)), 6000, rng))
    A_a = _matriz(doses_um, nomes, _amostra(anel, 6000, rng))
    w0 = np.array([c.mu for c in plano.campos], float)
    principal = np.array([c.campo_pai is None for c in plano.campos])
    lb = np.where(principal, frac_principal * w0, 0.0)
    w = _otimizar(A_alvo, [], A_q, A_a, w0, lb, rx, plano.prescricao.fracoes, reg=regularizacao)
    pesos_antes = {c.nome: c.peso for c in plano.campos}
    campos_antes = list(plano.campos)
    removidas = {}
    for c, x in zip(plano.campos, w):
        c.peso = float(max(x, 1e-4))
    avisos = []
    calculo.normalizar_sem_minimo(plano, doses_um, grade, alvo)
    for c in [c for c in plano.campos if c.campo_pai is not None and (c.mu or 0) < um_min]:
        avisos.append(f"{c.nome}: removido no ajuste fino ({c.mu:.1f} UM < {um_min:g})")
        plano.campos.remove(c)
        removidas[c.nome] = doses_um.pop(c.nome)
    d = calculo.normalizar(plano, doses_um, grade, alvo)
    hi1 = indice_homogeneidade(d, alvo)
    if hi1 > hi0:                                       # nunca piora: volta aos campos e pesos de partida
        plano.campos[:] = campos_antes
        doses_um.update(removidas)
        for c in plano.campos:
            c.peso = pesos_antes[c.nome]
        calculo.normalizar(plano, doses_um, grade, alvo)
        avisos = []
        avisos.append("Ajuste fino não melhorou a homogeneidade: pesos do filtro mantidos")
        hi1 = hi0
    return hi0, hi1, avisos
