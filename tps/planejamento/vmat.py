"""VMAT para Elekta (Precise MLCi e Synergy Agility): arcos com MLC dinâmico e taxa de dose variável. SRS-VMT-001.

Três etapas, como os sistemas comerciais (Monaco e Eclipse fazem variantes disso):
  1. Fluência por setor: o arco é dividido em setores (padrão 40°); cada setor vira um "campo" no meio do setor,
     conformado ao alvo, e as fluências de todos são otimizadas juntas com o mesmo custo do IMRT (objetivos
     ponderados ou restritos, serial, paralelo, tecido normal, avoidance e auto-flash).
  2. Sequenciamento em arco: a fluência de cada setor vira uma janela deslizante unidirecional distribuída pelos
     pontos de controle do setor; os setores alternam o sentido da varredura, então as lâminas terminam um setor
     onde o próximo começa. Dentro do setor a UM por intervalo é constante.
  3. Otimização direta dos pontos de controle: dose de cada intervalo pelo motor CCC (grade grossa), UM de cada
     intervalo (limites de UM/grau) e depois as posições das lâminas de todos os pontos juntos no modelo rápido
     calibrado pela dose real, com o percurso por grau penalizado (velocidade da lâmina × velocidade do gantry),
     gap mínimo e, no MLCi, sem interdigitação. Repete com a dose recalculada.
No fim, cada arco é calculado na grade do plano numa chamada só do motor (intervalos agrupados por setor de 10°).

Entre dois pontos de controle o Elekta move gantry e lâminas linearmente com a UM; o intervalo é calculado no
gantry do meio com a abertura média (calculo.intervalos_arco).
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import distance_transform_edt, gaussian_filter
from scipy.optimize import minimize

from tps.planejamento import calculo
from tps.planejamento import imrt as I
from tps.planejamento.ajuste_mlc import Margem, OpcoesAjuste, ajustar, verificar_mecanica
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano


@dataclass
class LimitesVMAT:
    gantry_vel_max_graus_s: float = 6.0
    lamina_vel_max_mm_s: float = 20.0
    taxa_dose_max_um_min: float = 500.0
    um_por_grau_min: float = 0.1
    um_por_grau_max: float = 20.0
    deslocamento_max_mm_grau: float = 4.0
    espacamento_cp_graus: float = 4.0
    max_pontos_arco: float = 360.0

    @classmethod
    def da_maquina(cls, raw: dict | None) -> "LimitesVMAT":
        d = (raw or {}).get("vmat") or {}
        return cls(**{k: float(v) for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ResultadoVMAT:
    arcos: dict                        # nome → {"pontos": n, "um": UM, "tempo_s": s, "lamina_mm_s": máx}
    um_total: float
    custo: list = field(default_factory=list)
    avisos: list = field(default_factory=list)
    tempo_s: float = 0.0
    restricoes: dict = field(default_factory=dict)
    etapas: dict = field(default_factory=dict)        # custo em cada etapa (diagnóstico)


# ------------------------------------------------------------------ geometria do arco
def comprimento_arco(inicio: float, fim: float, sentido: str) -> float:
    L = ((fim - inicio) if sentido == "CW" else (inicio - fim)) % 360.0
    return 360.0 if L < 1e-6 else L


def validar_arco(inicio: float, fim: float, sentido: str, limite_graus: float = 185.0) -> list[str]:
    """Elekta: o gantry anda de −185° a +185° (passa pelo 0, não dá volta pelo 180)."""
    if sentido not in ("CW", "CC"):
        return ["Sentido do arco deve ser CW ou CC"]
    L = comprimento_arco(inicio, fim, sentido)
    if sentido == "CW":
        s = (inicio + 180.0) % 360.0 - 180.0                 # [-180, 180)
        ok = s + L <= limite_graus + 1e-6
    else:
        s = 180.0 - (180.0 - inicio) % 360.0                 # (-180, 180]
        ok = s - L >= -limite_graus - 1e-6
    return [] if ok else [f"Arco {inicio:g}→{fim:g} {sentido} passa do limite do gantry (±{limite_graus:g}°)"]


def angulos_cp(inicio: float, fim: float, sentido: str, passo: float) -> np.ndarray:
    L = comprimento_arco(inicio, fim, sentido)
    n = max(int(np.ceil(L / passo - 1e-6)), 2)
    sinal = 1.0 if sentido == "CW" else -1.0
    return (inicio + sinal * np.linspace(0.0, L, n + 1)) % 360.0


def tempo_entrega(c: Campo, lim: LimitesVMAT, linhas: np.ndarray | None = None) -> dict:
    """Tempo do arco: em cada intervalo vale o mais lento entre gantry, taxa de dose e lâminas."""
    pc = c.pontos_controle
    mu = float(c.mu or c.peso or 0.0)
    total, por_lamina, vmax = 0.0, 0, 0.0
    for k in range(len(pc) - 1):
        p0, p1 = pc[k], pc[k + 1]
        dg = abs((float(p1["gantry"]) - float(p0["gantry"]) + 180.0) % 360.0 - 180.0)
        m = mu * (float(p1["mw"]) - float(p0["mw"]))
        da = np.abs(np.asarray(p1["mlc_a"]) - np.asarray(p0["mlc_a"]))
        db = np.abs(np.asarray(p1["mlc_b"]) - np.asarray(p0["mlc_b"]))
        if linhas is not None:
            da, db = da[linhas], db[linhas]
        curso = float(max(da.max(initial=0.0), db.max(initial=0.0)))
        t_g = dg / lim.gantry_vel_max_graus_s
        t_d = m / (lim.taxa_dose_max_um_min / 60.0)
        t_l = curso / lim.lamina_vel_max_mm_s
        t = max(t_g, t_d, t_l)
        if t_l >= t - 1e-9 and t_l > t_g:
            por_lamina += 1
        if t > 0:
            vmax = max(vmax, curso / t)
        total += t
    return {"tempo_s": round(total, 1), "intervalos_limitados_lamina": por_lamina, "lamina_mm_s": round(vmax, 1)}


# ------------------------------------------------------------------ mecânica de uma abertura
def reparar(a: np.ndarray, b: np.ndarray, colim: Colimacao, lo: float, hi: float, abertos=None) -> None:
    """Impõe (no lugar) gap mínimo, limites [lo, hi] e, sem interdigitação, A_i ≤ B_{i±1} − gap. Pares fechados
    (abertos[i] False) se deslocam inteiros; abertos cedem a lâmina que causa o conflito."""
    g = colim.gap_minimo_mm
    n = len(a)
    ab = np.ones(n, bool) if abertos is None else np.asarray(abertos, bool)
    for i in range(n):
        if b[i] - a[i] < g:
            c = 0.5 * (a[i] + b[i])
            a[i], b[i] = c - g / 2, c + g / 2
    np.clip(a, lo, hi - g, out=a)
    np.clip(b, lo + g, hi, out=b)
    if getattr(colim, "interdigitacao", True):
        _grade_01(a, b)
        return
    for _ in range(4 * n):
        mexeu = False
        for i in range(n - 1):
            for p, q in ((i, i + 1), (i + 1, i)):
                exc = a[p] - (b[q] - g)
                if exc <= 1e-6:
                    continue
                mexeu = True
                if not ab[q]:                                # o fechado anda para a direita
                    b[q] += exc
                    a[q] += exc
                elif not ab[p]:                              # o fechado anda para a esquerda
                    a[p] -= exc
                    b[p] -= exc
                else:                                        # dois abertos: metade para cada
                    a[p] -= exc / 2
                    b[q] += exc / 2
        np.clip(a, lo, hi - g, out=a)
        np.clip(b, lo + g, hi, out=b)
        if not mexeu:
            break
    # o que sobrar (par fechado entre duas aberturas que não se tocam): abre a lâmina A até cumprir — sempre resolve,
    # porque só diminuir A nunca cria conflito novo
    for i in range(n):
        viz = [b[j] for j in (i - 1, i, i + 1) if 0 <= j < n]
        a[i] = min(a[i], min(viz) - g)
    _grade_01(a, b)


def _grade_01(a, b) -> None:
    """Posições em 0,1 mm (resolução do RTPLAN) arredondando para abrir: não cria violação de gap nem de vizinhos."""
    a[:] = np.floor(a * 10 + 1e-6) / 10
    b[:] = np.ceil(b * 10 - 1e-6) / 10


# ------------------------------------------------------------------ sequenciamento do setor
def _tv(Q: np.ndarray) -> np.ndarray:
    z = np.zeros((Q.shape[0], 1))
    return np.maximum(np.diff(np.hstack([z, Q, z]), axis=1), 0.0).sum(axis=1)


def sequenciar_dinamico(Fc: np.ndarray, ab: np.ndarray, u: np.ndarray, n: int, sentido: int, colim: Colimacao,
                        lo: float, hi: float, fechados_em: np.ndarray | None = None,
                        folga: float = 0.35) -> tuple[list, float]:
    """Janela deslizante contínua (MLC dinâmico, Spirou & Chui) distribuída pelos n intervalos do setor.

    Em cada par, a lâmina da frente e a de trás percorrem a faixa do par sempre no mesmo sentido: onde a fluência
    sobe, a da frente anda no passo-base e a de trás atrasa o necessário; onde desce, o contrário. O tempo (em
    intervalos) entre as duas passarem por um ponto é a fluência ali, sem quantização em níveis. O par começa o setor
    fechado no início da faixa e termina fechado no fim — onde o setor seguinte (no sentido contrário) começa.
    `folga`: fração mínima do tempo do setor para o deslocamento-base (lâminas mais lentas, um pouco mais de UM).
    Devolve n + 1 aberturas (tempos 0..n; a última, fechada no fim da faixa, coincide com o primeiro ponto do setor
    seguinte) e a UM por intervalo em unidades de fluência."""
    res = float(u[1] - u[0])
    g = colim.gap_minimo_mm
    P = np.where(ab, np.maximum(Fc, 0.0), 0.0)
    nv, nu = P.shape
    if sentido < 0:
        P, abs_ = P[:, ::-1], ab[:, ::-1]
        s_col = -u[::-1]
    else:
        abs_, s_col = ab, u.astype(float)
    if not (P.max() > 0):
        return [], 0.0
    comum = not getattr(colim, "interdigitacao", True)   # MLCi: todos os pares na faixa do setor (vizinhos juntos)
    # MLCi: TODOS os pares andam juntos pela faixa do setor (os sem fluência fechados, na frente da varredura) —
    # assim a não-interdigitação quase não precisa de reparo. Agility: só os pares com fluência, na própria faixa
    linhas = list(range(nv)) if comum else [i for i in range(nv) if abs_[i].any() and P[i].max() > 0]
    # fluência nas bordas das células da faixa de cada par, e a subida total (TV+)
    perfis = {}
    todas = np.nonzero(abs_.any(axis=0))[0]
    for i in linhas:
        cc = todas if comum else np.nonzero(abs_[i])[0]
        j0, j1 = cc[0], cc[-1] + 1
        f = P[i, j0:j1]
        # fluência nas bordas das células; nas pontas da faixa vale a da célula (a lâmina de trás espera ali)
        G = np.r_[f[0], 0.5 * (f[1:] + f[:-1]), f[-1]]
        perfis[i] = (s_col[j0] - res / 2, G)
    tv = max(float(np.maximum(np.diff(G), 0).sum() + G[0]) for _, G in perfis.values())
    delta = tv / (n * (1.0 - folga))                     # fluência por intervalo
    if fechados_em is None:
        fechados_em = np.full(nv, 0.5 * (lo + hi))
    parado = np.asarray(fechados_em, float) * (1 if sentido > 0 else -1)
    A = np.repeat(parado[None, :] - g / 2, n + 1, axis=0)
    B = np.repeat(parado[None, :] + g / 2, n + 1, axis=0)
    aberto = np.zeros((n + 1, nv), bool)
    ts = np.arange(n + 1, dtype=float)
    for i in linhas:
        x0, G = perfis[i]
        G = G / delta                                     # em intervalos
        dG = np.diff(G)
        sobe = float(np.maximum(dG, 0).sum() + G[0])
        ncel = len(dG)
        c = (n - sobe) / ncel                             # passo-base (intervalos por célula)
        tL = np.r_[0.0, np.cumsum(c + np.maximum(-dG, 0.0))]   # lâmina da frente
        tT = tL + G                                       # a de trás passa G intervalos depois
        bordas = x0 + res * np.arange(ncel + 1)
        frente = np.interp(ts, tL, bordas)
        tras = np.interp(ts, tT, bordas)
        a, b = tras, frente
        estreito = b - a < g
        cc_ = 0.5 * (a + b)
        a = np.where(estreito, cc_ - g / 2, a)
        b = np.where(estreito, cc_ + g / 2, b)
        A[:, i], B[:, i] = a, b
        aberto[:, i] = True                                # em movimento: o reparo só ajusta as lâminas
    out = []
    for t in range(n + 1):
        if sentido < 0:
            a, b = -B[t].copy(), -A[t].copy()
        else:
            a, b = A[t].copy(), B[t].copy()
        reparar(a, b, colim, lo, hi, aberto[t])
        out.append((a, b))
    return out, delta


def sequenciar_setor(Fc: np.ndarray, ab: np.ndarray, u: np.ndarray, n: int, sentido: int, colim: Colimacao,
                     lo: float, hi: float, fechados_em: np.ndarray | None = None, compensar: int = 4) -> tuple[list, float]:
    """Sequenciamento com compensação: o que as aberturas entregam a mais (fendas de gap mínimo dos pares que
    esperam fechados dentro do campo, aberturas alargadas até o gap mínimo, ajustes de não-interdigitação) é
    descontado da fluência pedida e o setor é sequenciado de novo (ponto fixo); fica o de menor erro."""
    res = float(u[1] - u[0])
    pedido = np.where(ab, np.maximum(Fc, 0.0), 0.0)
    alvo = pedido.copy()
    melhor = None
    for it in range(compensar + 1):
        ap, delta = _sequenciar_setor(alvo, ab, u, n, sentido, colim, lo, hi, fechados_em)
        if not ap:
            return ap, delta
        entregue = sum(I._cobertura(a, b, u, res)[0] for a, b in ap) * delta * ab
        erro = entregue - pedido
        e2 = float((erro * erro).sum())
        if melhor is None or e2 < melhor[0]:
            melhor = (e2, ap, delta)
        if it == compensar or e2 < 1e-12:
            break
        alvo = np.maximum(alvo - erro, 0.0)
    return melhor[1], melhor[2]


def _sequenciar_setor(Fc: np.ndarray, ab: np.ndarray, u: np.ndarray, n: int, sentido: int, colim: Colimacao,
                      lo: float, hi: float, fechados_em: np.ndarray | None = None) -> tuple[list, float]:
    """Fluência do setor → n aberturas (a, b) e o nível de fluência de cada abertura. Pares com perfil de um pico só:
    close-in (sentido +1: do mais largo ao mais estreito; −1: o contrário). Os outros: janela deslizante
    unidirecional (sentido +1: da esquerda para a direita). Nos pontos sem intervalo o par espera fechado no
    começo/fim da sua faixa (ou no pico, no close-in). Hoje o padrão é sequenciar_dinamico; este fica como opção.
    Pares sem fluência no setor ficam parados, fechados em `fechados_em` (mm no BEV; padrão: centro)."""
    res = float(u[1] - u[0])
    g = colim.gap_minimo_mm
    P = np.where(ab, np.maximum(Fc, 0.0), 0.0)
    nv, nu = P.shape
    if sentido < 0:
        P = P[:, ::-1]
        s_col = -u[::-1]                                   # coordenada no sentido da varredura (crescente)
    else:
        s_col = u.astype(float)
    tv = _tv(P)
    if tv.max() <= 1e-9:
        return [], 0.0
    delta = float(tv.max()) / n
    for _ in range(60):
        Q = np.rint(P / delta).astype(int)
        if _tv(Q).max() <= n:
            break
        delta *= 1.04
    linhas_ab = set(np.nonzero(ab.any(axis=1))[0].tolist())
    abs_ = ab[:, ::-1] if sentido < 0 else ab
    if fechados_em is None:
        fechados_em = np.full(nv, 0.5 * (lo + hi))
    parado = np.asarray(fechados_em, float) * (1 if sentido > 0 else -1)      # no referencial da varredura
    A = np.zeros((n, nv))
    B = np.zeros((n, nv))
    aberto = np.zeros((n, nv), bool)
    for i in range(nv):
        if i not in linhas_ab or int(Q[i].max()) == 0:      # par sem fluência: parado, fechado
            A[:, i], B[:, i] = parado[i] - g / 2, parado[i] + g / 2
            continue
        qmax = int(Q[i].max())
        cc = np.nonzero(abs_[i])[0]
        s_ini, s_fim = s_col[cc[0]] - res / 2, s_col[cc[-1]] + res / 2
        um_pico = _tv(Q[i:i + 1])[0] == qmax
        if um_pico:
            # perfil de um pico só: close-in (conjuntos de nível, do mais largo ao mais estreito ou ao contrário)
            idx_ = []
            for t in range(1, qmax + 1):
                c_ = np.nonzero(Q[i] >= t)[0]
                idx_.append((c_[0], c_[-1] + 1))
            if sentido < 0:
                idx_ = idx_[::-1]
        else:
            idx_ = I.intervalos_sweep(Q[i])                  # janela deslizante (Bortfeld), em ordem
        jan = [(s_col[je] - res / 2, s_col[jd - 1] + res / 2) for je, jd in idx_]
        m = len(jan)
        t0 = (n - m) // 2 if not um_pico else (0 if sentido > 0 else n - m)
        if um_pico:
            je, jd = idx_[-1] if sentido > 0 else idx_[0]
            espera = 0.5 * (s_col[je] - res / 2 + s_col[jd - 1] + res / 2)
        for t in range(n):
            k = t - t0
            if 0 <= k < m:
                e, d = jan[k]
                if d - e < g:
                    c_ = 0.5 * (e + d)
                    e, d = c_ - g / 2, c_ + g / 2
                A[t, i], B[t, i] = e, d
                aberto[t, i] = True
            elif um_pico:
                A[t, i], B[t, i] = espera - g / 2, espera + g / 2
            elif k >= m:
                A[t, i], B[t, i] = s_fim - g, s_fim
            else:
                A[t, i], B[t, i] = s_ini, s_ini + g
    out = []
    for t in range(n):
        if sentido < 0:                                      # de volta às coordenadas do BEV
            a, b = -B[t].copy(), -A[t].copy()
        else:
            a, b = A[t].copy(), B[t].copy()
        reparar(a, b, colim, lo, hi, aberto[t])
        out.append((a, b))
    return out, delta


# ------------------------------------------------------------------ modelo rápido por intervalo
class ModeloIntervalos:
    """Dose rápida de cada intervalo entre pontos de controle no seu próprio gantry:
    D = correção + Σ_j UM_j · d_j · interp(G ∗ cobertura_j), d_j = dose/UM do campo aberto do setor nesse gantry
    (motor CCC) e cobertura_j = fração de cada célula (par × coluna) aberta pela abertura média do intervalo. Fora da
    abertura do setor a célula vale a vizinha de dentro (sem penumbra dupla, como no IMRT)."""

    def __init__(self, arcos, mapas, abertos: list[Campo], d0: list[np.ndarray], grade, idx, sigma_px, posicao):
        self.mapas = mapas
        self.sigma = sigma_px
        self.esp = 0.0                   # fração de espalhamento (kernel largo)
        self.sigma2 = {}
        self.n = len(idx)
        self.corr = np.zeros(self.n)
        self.perto = {}
        for nome, (u, v, ab) in mapas.items():
            _, (ii, jj) = distance_transform_edt(~ab, return_indices=True)
            self.perto[nome] = (ii * len(u) + jj).ravel()
        self.itens = []
        for k, arco in enumerate(arcos):
            for j, (o, s_nome) in enumerate(zip(arco["ofs_w"], arco["setor_int"])):
                u, v, ab = mapas[s_nome]
                i, j0, t, ok = I._raios(grade, idx, abertos[o], u, v, posicao)
                dd = d0[o] * ok
                sel = np.nonzero(dd > 1e-5 * max(float(dd.max()), 1e-30))[0].astype(np.int32)
                k0 = (i * len(u) + j0)[sel].astype(np.int32)
                self.itens.append((k, j, o, s_nome, sel, k0, t[sel].astype(np.float32), dd[sel].astype(np.float32)))

    def espalhamento(self, fracao: float, alcance_mm: float) -> None:
        """D ∝ (1−f)·G_σ ∗ cobertura + f·G_largo ∗ cobertura: com f > 0, aberturas pequenas recebem menos
        espalhamento que o campo aberto em que d_j foi calculado."""
        self.esp = float(fracao)
        for nome, (u, v, ab) in self.mapas.items():
            dv = float(np.mean(np.abs(np.diff(v)))) if len(v) > 1 else 5.0
            self.sigma2[nome] = (alcance_mm / dv, alcance_mm / float(u[1] - u[0]))

    def _borrar(self, x, s_nome):
        y = gaussian_filter(x, self.sigma, mode="nearest")
        if self.esp > 0:
            y = (1 - self.esp) * y + self.esp * gaussian_filter(x, self.sigma2[s_nome], mode="nearest")
        return y

    def avaliar(self, Ak, Bk, w, custo=None):
        """(custo, gA, gB, gw, D); sem custo devolve só a dose."""
        D = self.corr.copy()
        cache = []
        for (k, j, o, s_nome, sel, k0, t, dd) in self.itens:
            u, v, ab = self.mapas[s_nome]
            a = 0.5 * (Ak[k][j] + Ak[k][j + 1])
            b = 0.5 * (Bk[k][j] + Bk[k][j + 1])
            m, da, db = I._cobertura(a, b, u, u[1] - u[0])
            nn = m.size
            Fe = self._borrar(m.ravel()[self.perto[s_nome]].reshape(m.shape), s_nome).ravel()
            k1 = np.minimum(k0 + 1, nn - 1)
            val = (1 - t) * Fe[k0] + t * Fe[k1]
            D[sel] += w[o] * dd * val
            cache.append((da, db, val, k1, m.shape))
        if custo is None:
            return D
        f, gD = custo(D)
        gw = np.zeros(len(w))
        gA = [np.zeros_like(A) for A in Ak]
        gB = [np.zeros_like(B) for B in Bk]
        for (k, j, o, s_nome, sel, k0, t, dd), (da, db, val, k1, shape) in zip(self.itens, cache):
            gx = gD[sel] * dd
            gw[o] = float(gx @ val)
            nn = shape[0] * shape[1]
            ge = np.zeros(nn)
            np.add.at(ge, k0, w[o] * gx * (1 - t))
            np.add.at(ge, k1, w[o] * gx * t)
            ge = self._borrar(ge.reshape(shape), s_nome).ravel()
            gm = np.zeros(nn)
            np.add.at(gm, self.perto[s_nome], ge)
            gm = gm.reshape(shape)
            ga = 0.5 * (gm * da).sum(axis=1)
            gb = 0.5 * (gm * db).sum(axis=1)
            gA[k][j] += ga
            gA[k][j + 1] += ga
            gB[k][j] += gb
            gB[k][j + 1] += gb
        return f, gA, gB, gw, D


def otimizar_pontos(arcos, Acp, Bcp, w, modelo: ModeloIntervalos, custo, colim, lim: LimitesVMAT, linhas_arco,
                    limites_x, faixas=None, maxiter: int = 120, passo_mm: float = 10.0):
    """Posições das lâminas em todos os pontos de controle e UM dos intervalos juntos, no modelo rápido por intervalo
    calibrado pela dose real, numa região de confiança de ±passo_mm. Penalidades: gap, (MLCi) interdigitação e
    percurso por grau acima do limite (que só alonga a entrega)."""
    g = colim.gap_minimo_mm
    interdig = getattr(colim, "interdigitacao", True)
    lay = []                                                  # por arco: (ofs, n_cp, nL)
    x0, bnds = [], []
    for k in range(len(arcos)):
        L = linhas_arco[k]
        ncp = Acp[k].shape[0]
        lay.append((len(x0), ncp, len(L)))
        lo, hi = limites_x[k]
        blk = np.stack([Acp[k][:, L], Bcp[k][:, L]], axis=-1).ravel()
        x0 += blk.tolist()
        if faixas is None:
            bnds += [(lo, hi - g), (lo + g, hi)] * (ncp * len(L))
        else:
            for j in range(ncp):
                for i in L:
                    e = faixas[k][j, i]
                    if np.isnan(e[0]):                       # par fechado neste setor: parado
                        bnds += [(Acp[k][j, i], Acp[k][j, i]), (Bcp[k][j, i], Bcp[k][j, i])]
                    else:
                        e0, e1 = max(e[0], lo), min(e[1], hi)
                        bnds += [(e0, max(e1 - g, e0)), (min(e0 + g, e1), e1)]
    nX = len(x0)
    # região de confiança: o modelo rápido só vale perto das aberturas em que foi calibrado
    bnds = [(max(lo_, x - passo_mm), min(hi_, x + passo_mm)) if lo_ <= x <= hi_ else (lo_, hi_)
            for (lo_, hi_), x in zip(bnds, x0)]
    x0 = np.r_[np.asarray(x0, float), np.asarray(w, float)]
    for arco in arcos:
        for dg in arco["dtheta"]:
            bnds.append((lim.um_por_grau_min * dg, lim.um_por_grau_max * dg))
    x0 = np.clip(x0, [b[0] for b in bnds], [b[1] if b[1] is not None else np.inf for b in bnds])
    rho = 1e-2

    def desempacotar(x):
        Ak, Bk = [], []
        for k, (o, ncp, nL) in enumerate(lay):
            blk = x[o:o + 2 * ncp * nL].reshape(ncp, nL, 2)
            A, B = Acp[k].copy(), Bcp[k].copy()
            A[:, linhas_arco[k]] = blk[..., 0]
            B[:, linhas_arco[k]] = blk[..., 1]
            Ak.append(A)
            Bk.append(B)
        return Ak, Bk

    def fun(x):
        Ak, Bk = desempacotar(x)
        wv = x[nX:]
        f, gA_t, gB_t, gw, _ = modelo.avaliar(Ak, Bk, wv, custo)
        gx = np.zeros_like(x)
        gx[nX:] = gw
        gA = [gA_t[k][:, linhas_arco[k]] for k in range(len(lay))]
        gB = [gB_t[k][:, linhas_arco[k]] for k in range(len(lay))]
        pen = 0.0
        for k, arco in enumerate(arcos):
            L = linhas_arco[k]
            A, B = Ak[k][:, L], Bk[k][:, L]
            pA, pB = np.zeros_like(A), np.zeros_like(B)
            e = np.maximum(A - B + g, 0.0)                                   # gap
            pen += float((e * e).sum())
            pA += 2 * e
            pB -= 2 * e
            if not interdig and len(L) > 1:                                  # MLCi
                viz = np.nonzero(np.diff(L) == 1)[0]
                for j0, j1 in ((viz, viz + 1), (viz + 1, viz)):
                    e2 = np.maximum(A[:, j0] - B[:, j1] + g, 0.0)
                    pen += float((e2 * e2).sum())
                    pA[:, j0] += 2 * e2
                    pB[:, j1] -= 2 * e2
            dmax = lim.deslocamento_max_mm_grau * np.asarray(arco["dtheta"])[:, None]
            for M, pM in ((A, pA), (B, pB)):                                  # percurso por grau
                d = np.diff(M, axis=0)
                ex = np.maximum(np.abs(d) - dmax, 0.0) * np.sign(d)
                pen += float((ex * ex).sum())
                pM[1:] += 2 * ex
                pM[:-1] -= 2 * ex
            o, ncp, nL = lay[k]
            gx[o:o + 2 * ncp * nL] = np.stack([gA[k] + rho * pA, gB[k] + rho * pB], axis=-1).ravel()
        return f + rho * pen, gx

    r = minimize(fun, x0, jac=True, method="L-BFGS-B", bounds=bnds,
                 options={"maxiter": maxiter, "ftol": 1e-13, "gtol": 1e-11})
    Ak, Bk = desempacotar(r.x)
    return Ak, Bk, np.asarray(r.x[nX:], float)


def _mesmos_pontos(p1, p2) -> bool:
    """Mesmos pontos de controle (mesma geometria e mesmas frações de UM): a dose/UM do arco é a mesma."""
    if len(p1) != len(p2):
        return False
    for a, b in zip(p1, p2):
        if a["gantry"] != b["gantry"] or a["mlc_a"] != b["mlc_a"] or a["mlc_b"] != b["mlc_b"] or \
                a["jaw_y"] != b["jaw_y"] or a.get("jaw_x") != b.get("jaw_x") or abs(a["mw"] - b["mw"]) > 1e-9:
            return False
    return True


def _pontos_controle(arco, A, B, wk, jaws_k) -> list[dict]:
    jx, jy = jaws_k
    tot = float(np.sum(wk))
    mw = np.r_[0.0, np.cumsum(wk) / max(tot, 1e-12)]
    mw[-1] = 1.0
    return [{"gantry": round(float(arco["angs"][j]), 2), "mlc_a": [round(float(x), 1) for x in A[j]],
             "mlc_b": [round(float(x), 1) for x in B[j]], "jaw_x": list(jx) if jx else None, "jaw_y": list(jy),
             "mw": round(float(mw[j]), 6)} for j in range(len(arco["angs"]))]


# ------------------------------------------------------------------ otimização completa
def otimizar_vmat(plano: Plano, grade: calculo.GradeDose, doses_um: dict, objetivos: list, corpo: np.ndarray,
                  colim: Colimacao, modelo, limites: LimitesVMAT | None = None, setor_graus: float = 40.0,
                  iteracoes: int = 2, margem_mm: float = 5.0, res_u_mm: float = 2.5, sigma_mm: tuple = (3.0, 3.5),
                  peso_nto: float = 1.0, suavidade: float = 2.0, kernels=None, posicao: str = "HFS",
                  permitir_demonstracao: bool = False, progresso=None, evitar: list | None = None,
                  flash_mm: float = 0.0, flash_pele_mm: float = 5.0, grade_grossa: bool = True,
                  polimento_fino: bool = True, passo_aberto: int = 2,
                  espacamento_cp_graus: float | None = None, segurar: bool = False, um_exata: bool = True,
                  dinamico: bool = True, folga: float = 0.35,
                  _so_sequenciar: bool = False) -> ResultadoVMAT:
    """Otimiza os arcos do plano (campos com arco_fim/sentido). objetivos: imrt.Objetivo. evitar: [(nome, máscara,
    modo)]; flash_mm > 0 liga o auto-flash."""
    t0 = time.time()
    plano._exigir_rascunho()
    aviso = progresso or (lambda m: None)
    lim = copy.copy(limites or LimitesVMAT())
    if espacamento_cp_graus:
        lim.espacamento_cp_graus = float(espacamento_cp_graus)
    principais = [c for c in plano.campos if c.campo_pai is None]
    arcos_c = [c for c in principais if c.e_arco]
    if not arcos_c:
        raise ValueError("Crie os arcos antes de otimizar o VMAT")
    if len(arcos_c) != len(principais):
        raise ValueError("VMAT: o plano só pode ter arcos (remova os campos estáticos)")
    alvos = [o for o in objetivos if o.tipo == "alvo"]
    if not alvos:
        raise ValueError("Defina pelo menos um alvo com dose")
    for o in objetivos:
        if o.tipo not in I.TIPOS:
            raise ValueError(f"Tipo de objetivo desconhecido: {o.tipo}")
    fx = plano.prescricao.fracoes
    rng = np.random.default_rng(0)
    avisos = []
    todos_alvos = np.zeros_like(corpo)
    for o in alvos:
        todos_alvos |= o.mascara
    eixos = (grade.x, grade.y, grade.z)

    # ---- setores de cada arco: um campo conformado no meio de cada setor
    aviso("Montando os setores dos arcos…")
    arcos, setores = [], []
    for c in arcos_c:
        erros = validar_arco(c.gantry, c.arco_fim, c.sentido)
        if erros:
            raise ValueError("; ".join(erros))
        c.segmentos = None
        angs = angulos_cp(c.gantry, c.arco_fim, c.sentido, lim.espacamento_cp_graus)
        N = len(angs) - 1
        passo = comprimento_arco(c.gantry, c.arco_fim, c.sentido) / N
        sinal = 1.0 if c.sentido == "CW" else -1.0
        n_set = max(1, int(round(setor_graus / passo)))
        mg = float(c.margem_mm if c.margem_mm is not None else margem_mm)
        lista, setor_int = [], []
        for s0 in range(0, N, n_set):
            s1 = min(N, s0 + n_set)
            meio = (angs[s0] + sinal * passo * (s1 - s0) / 2) % 360.0
            cs = Campo(f"{c.nome}~{len(lista):02d}", round(float(meio), 2), c.colimador, c.mesa, c.isocentro_mm,
                       energia_mv=c.energia_mv)
            ajustar(cs, colim, [(todos_alvos, Margem.uniforme(mg))], [], eixos, OpcoesAjuste("fora"), posicao=posicao)
            lista.append((s0, s1, cs))
            setor_int += [cs.nome] * (s1 - s0)
        arcos.append({"campo": c, "angs": angs, "dtheta": np.full(N, passo), "setores": lista,
                      "setor_int": setor_int, "sinal": sinal})
        setores += [cs for _, _, cs in lista]
    if flash_mm > 0:
        com = [cs.nome for cs in setores if I.aplicar_flash(cs, colim, grade, corpo, todos_alvos, flash_mm,
                                                             flash_pele_mm, posicao)]
        if com:
            avisos.append(f"Auto-flash de {flash_mm:g} mm em {len(com)} de {len(setores)} setores")
    o = 0
    for arco in arcos:
        n = len(arco["setor_int"])
        arco["ofs_w"] = list(range(o, o + n))
        o += n
    nW = o
    # limites das lâminas por arco e jaws fixos do arco (envelope dos setores)
    limites_x, jaws = [], []
    for arco in arcos:
        cs_l = [cs for _, _, cs in arco["setores"]]
        abre = [np.asarray(cs.mlc_b) - np.asarray(cs.mlc_a) > colim.gap_minimo_mm + 0.5 for cs in cs_l]
        lo = min(float(np.asarray(cs.mlc_a)[m].min()) for cs, m in zip(cs_l, abre) if m.any()) - 5.0
        hi = max(float(np.asarray(cs.mlc_b)[m].max()) for cs, m in zip(cs_l, abre) if m.any()) + 5.0
        lo, hi = max(lo, -colim.lamina_max_mm), min(hi, colim.lamina_max_mm)
        limites_x.append((lo, hi))
        jy = (min(cs.jaw_y[0] for cs in cs_l), max(cs.jaw_y[1] for cs in cs_l))
        jx = None
        if colim.tem_jaw_x:
            jx = (max(min(cs.jaw_x[0] for cs in cs_l), lo), min(max(cs.jaw_x[1] for cs in cs_l), hi))
        jaws.append((jx, jy))

    # ---- amostras, custo e tecido normal (como no IMRT)
    passo_g = float(abs(grade.x[1] - grade.x[0]))
    dist = distance_transform_edt(~todos_alvos, sampling=(abs(grade.z[1] - grade.z[0]), abs(grade.y[1] - grade.y[0]),
                                                           passo_g))
    rx_max = max(o.dose_gy for o in alvos)
    normal = corpo & (dist > 3.0)
    mascs = [o.mascara for o in objetivos] + [normal]
    nmax = [15000 if o.tipo == "alvo" else 6000 for o in objetivos] + [20000]
    idx, partes = I._amostras(mascs, nmax, rng)
    lim_nto = rx_max * (0.55 + 0.50 * np.exp(-0.05 * (dist.ravel()[idx[partes[-1]]] - 3.0)))
    custo = I.Custo(objetivos, partes[:-1], (partes[-1], lim_nto, peso_nto) if peso_nto > 0 else None)
    kz, ky, kx = np.unravel_index(idx, grade.eta.shape)
    pts = (grade.x[kx], grade.y[ky], grade.z[kz])
    gg = I._grade_grossa(grade) if grade_grossa and passo_g < 4.5 else grade

    def amostrar(lista_campos, rotulo, rapido=False):
        out = {}
        for j, cs in enumerate(lista_campos):
            if j % 15 == 0:
                aviso(f"{rotulo}… {j}/{len(lista_campos)}")
            d = calculo.dose_por_um(gg, [cs], colim, modelo, kernels, posicao, permitir_demonstracao,
                                    rapido=rapido)[cs.nome]
            out[cs.nome] = I._amostrar_grossa(d, gg, pts)
        return out

    mapas = {cs.nome: I._grade_fluencia(cs, colim, res_u_mm) for cs in setores}
    if evitar:
        bloq = 0
        for cs in setores:
            u, v, ab = mapas[cs.nome]
            bq = I.bloqueio_avoidance(grade, cs, u, v, posicao, todos_alvos, [(m, modo) for _, m, modo in evitar])
            mapas[cs.nome] = (u, v, ab & ~bq)
            bloq += int((ab & bq).sum())
        if bloq:
            avisos.append(f"Avoidance bloqueou {bloq} células de fluência nos setores")
    zona_flash = {}
    if flash_mm > 0:
        from tps.planejamento.ajuste_mlc import bev_da_estrutura
        for cs in setores:
            u, v, ab = mapas[cs.nome]
            zona_flash[cs.nome] = ab & ~bev_da_estrutura(corpo, eixos, cs, u, v, posicao=posicao).m
    mu0 = 100.0
    abertos = amostrar(setores, "Calculando os setores abertos")
    d_ab = {cs.nome: fx * mu0 * abertos[cs.nome] for cs in setores}
    limites_lam = np.asarray(colim.limites_laminas)
    sig = (sigma_mm[0] / float(limites_lam[1] - limites_lam[0]), sigma_mm[1] / res_u_mm)
    mod = I.ModeloFluencia(setores, mapas, grade, idx, d_ab, sig, posicao)
    blocos = {cs.nome: bl for cs, bl in zip(setores, mod.blocos)}
    livres = mod.livres()
    for cs in setores:
        if cs.nome in zona_flash:
            bl = blocos[cs.nome]
            livres[bl["ofs"]:bl["ofs"] + bl["n"]] &= ~zona_flash[cs.nome].ravel()

    # ---- 1. fluência por setor
    F = np.where(livres, 1.0, 0.0)
    D0 = mod.dose(F)
    a0 = alvos[0]
    F *= a0.dose_gy / max(float(D0[partes[objetivos.index(a0)]].mean()), 1e-9)

    def suav(Fv):
        f, gr = 0.0, np.zeros_like(Fv)
        for bl in mod.blocos:
            Fc = Fv[bl["ofs"]:bl["ofs"] + bl["n"]].reshape(bl["shape"])
            ab = bl["ab"]
            du = np.diff(Fc, axis=1) * (ab[:, 1:] & ab[:, :-1])
            dv = np.diff(Fc, axis=0) * (ab[1:] & ab[:-1])
            nrm = max(ab.sum(), 1)
            f += suavidade * (float((du ** 2).sum()) + 0.5 * float((dv ** 2).sum())) / nrm
            gs = np.zeros_like(Fc)
            gs[:, 1:] += 2 * suavidade * du / nrm
            gs[:, :-1] -= 2 * suavidade * du / nrm
            gs[1:] += suavidade * dv / nrm
            gs[:-1] -= suavidade * dv / nrm
            gr[bl["ofs"]:bl["ofs"] + bl["n"]] = gs.ravel()
        return f, gr

    aviso("Otimizando a fluência dos setores…")

    def obj(Fv):
        f, gD = custo(mod.dose(Fv))
        fs, gs = suav(Fv)
        return f + fs, (mod.grad(gD) + gs) * livres
    F = I._minimizar_al(obj, F, [(0.0, None) if lv else (0.0, 0.0) for lv in livres], custo, mod.dose, 4, 400, aviso)
    etapas = {"fluencia": round(float(custo(mod.dose(F))[0]), 5)}

    # ---- 2. sequenciamento em arco
    aviso("Sequenciando os arcos…")
    Acp, Bcp, w = [], [], np.zeros(nW)
    Fquant, Fslot = np.zeros(mod.n), np.zeros(mod.n)
    _dump = []
    for k, arco in enumerate(arcos):
        N = len(arco["setor_int"])
        A = np.zeros((N + 1, colim.n_pares))
        B = np.zeros((N + 1, colim.n_pares))
        lo, hi = limites_x[k]
        parado = np.full(colim.n_pares, 0.5 * (lo + hi))
        fim_setor = None
        for q, (s0, s1, cs) in enumerate(arco["setores"]):
            bl = blocos[cs.nome]
            u, v, ab = mapas[cs.nome]
            Fc = np.where(ab, F[bl["ofs"]:bl["ofs"] + bl["n"]].reshape(bl["shape"]), 0.0)
            zf = zona_flash.get(cs.nome)
            if zf is not None and zf.any() and (ab & ~zf).any():
                _, (ii, jj) = distance_transform_edt(~(ab & ~zf), return_indices=True)
                Fc = np.where(zf, Fc[ii, jj], Fc)
            nint = s1 - s0
            nslots = max(nint // 2, 1) if segurar else nint
            if dinamico and not segurar:
                ap, delta = sequenciar_dinamico(Fc, ab, u, nslots, 1 if q % 2 == 0 else -1, colim, lo, hi, parado,
                                                folga=folga)
            else:
                ap, delta = sequenciar_setor(Fc, ab, u, nslots, 1 if q % 2 == 0 else -1, colim, lo, hi, parado)
            if _so_sequenciar:
                _dump.append((Fc.copy(), ab.copy(), u.copy(), s1 - s0, 1 if q % 2 == 0 else -1, lo, hi,
                              None if parado is None else np.array(parado)))
            if _so_sequenciar and ap:                          # diagnóstico: quantização e aberturas sem média
                Fquant[bl["ofs"]:bl["ofs"] + bl["n"]] = (np.rint(Fc / delta) * delta * ab).ravel()
                Fslot[bl["ofs"]:bl["ofs"] + bl["n"]] = (sum(I._cobertura(a, b, u, u[1] - u[0])[0] for a, b in ap)
                                                        * delta * ab).ravel()
            if not ap:                                         # setor sem fluência: fechado no centro
                cc = 0.5 * (lo + hi)
                ap = [(np.full(colim.n_pares, cc - colim.gap_minimo_mm / 2),
                       np.full(colim.n_pares, cc + colim.gap_minimo_mm / 2))] * (s1 - s0)
            if segurar:
                # cada abertura fica parada em dois pontos seguidos (o intervalo entre eles entrega exatamente a
                # abertura); no intervalo seguinte as lâminas andam para a próxima com a UM mínima
                for t in range(nint):
                    a, b = ap[min(t // 2, len(ap) - 1)]
                    A[s0 + t], B[s0 + t] = a, b
                    hold = t % 2 == 0 and t // 2 < len(ap)
                    w[arco["ofs_w"][s0 + t]] = delta * mu0 if hold else 0.0
            else:
                for t in range(nint):
                    A[s0 + t], B[s0 + t] = ap[min(t, len(ap) - 1)]
                    w[arco["ofs_w"][s0 + t]] = delta * mu0
                if len(ap) > nint:                             # dinâmico: estado final (fechado no fim da faixa)
                    fim_setor = ap[nint]
            parado = 0.5 * (ap[-1][0] + ap[-1][1])
        if dinamico and not segurar and fim_setor is not None:
            A[N], B[N] = fim_setor
        else:
            A[N], B[N] = A[N - 1], B[N - 1]
        Acp.append(A)
        Bcp.append(B)
    w = np.clip(w, np.concatenate([lim.um_por_grau_min * a["dtheta"] for a in arcos]),
                np.concatenate([lim.um_por_grau_max * a["dtheta"] for a in arcos]))
    # faixa de cada par em cada ponto de controle (abertura do setor do ponto); fora dela o modelo rápido não enxerga
    # a dose, então a otimização direta não pode abrir ali; pares fechados no setor ficam parados
    faixas = []
    for k, arco in enumerate(arcos):
        N = len(arco["setor_int"])
        fx_ = np.full((N + 1, colim.n_pares, 2), np.nan)
        for s0, s1, cs in arco["setores"]:
            u, v, ab = mapas[cs.nome]
            res = u[1] - u[0]
            env = np.full((colim.n_pares, 2), np.nan)
            for i in np.nonzero(ab.any(axis=1))[0]:
                cc = np.nonzero(ab[i])[0]
                env[i] = (u[cc[0]] - res / 2, u[cc[-1]] + res / 2)
            fx_[s0:s1 + (1 if s1 == N else 0)] = env
        faixas.append(fx_)
    linhas_arco = []
    for arco in arcos:
        rows = np.zeros(colim.n_pares, bool)
        for _, _, cs in arco["setores"]:
            rows |= mapas[cs.nome][2].any(axis=1)
        linhas_arco.append(np.nonzero(rows)[0])

    # diagnóstico: perda do sequenciamento no modelo de fluência do setor (sem o espalhamento angular do setor)
    Fq = np.zeros(mod.n)
    for k, arco in enumerate(arcos):
        for j, (o, s_nome) in enumerate(zip(arco["ofs_w"], arco["setor_int"])):
            u, v, ab = mapas[s_nome]
            bl = blocos[s_nome]
            m, _, _ = I._cobertura(0.5 * (Acp[k][j] + Acp[k][j + 1]), 0.5 * (Bcp[k][j] + Bcp[k][j + 1]), u,
                                   u[1] - u[0])
            Fq[bl["ofs"]:bl["ofs"] + bl["n"]] += (w[o] / mu0 * m * ab).ravel()
    etapas["sequenciada_modelo_setor"] = round(float(custo(mod.dose(Fq))[0]), 5)
    if _so_sequenciar:
        etapas["quantizada"] = round(float(custo(mod.dose(Fquant))[0]), 5)
        etapas["aberturas_sem_media"] = round(float(custo(mod.dose(Fslot))[0]), 5)
        etapas["_setores"] = _dump
        return ResultadoVMAT({}, 0.0, [], avisos, round(time.time() - t0, 1), {}, etapas)

    # ---- 3. otimização direta dos pontos de controle
    # A cada iteração o motor calcula a dose TOTAL de cada arco numa chamada só (intervalos agrupados em setores de 10°,
    # cones reduzidos, grade grossa); o modelo rápido por intervalo dá os gradientes e é recalibrado (correção aditiva)
    # pela dose do motor. Folhas e UM de todos os pontos são otimizadas juntas.
    def arcos_temporarios(Ak, Bk, wv):
        out = []
        for k, arco in enumerate(arcos):
            c = copy.copy(arco["campo"])
            c.pontos_controle = _pontos_controle(arco, Ak[k], Bk[k], wv[arco["ofs_w"]], jaws[k])
            c.mu = float(wv[arco["ofs_w"]].sum())
            out.append(c)
        return out

    cache_fino = {}                                          # nome → (pontos de controle, dose/UM na grade do plano)

    def dose_total(Ak, Bk, wv, g_, rapido, rotulo):
        aviso(rotulo)
        D = np.zeros(len(idx))
        for c in arcos_temporarios(Ak, Bk, wv):
            d = calculo.dose_por_um(g_, [c], colim, modelo, kernels, posicao, permitir_demonstracao,
                                    rapido=rapido)[c.nome]
            if g_ is gr_fina and not rapido:
                cache_fino.setdefault(c.nome, []).append((c.pontos_controle, d))
            D += fx * c.mu * (I._amostrar_grossa(d, g_, pts) if g_ is not gr_fina else d.ravel()[idx])
        return D

    gr_fina = grade
    # modelo rápido: campo aberto do setor no gantry dos intervalos (um cálculo a cada `passo_aberto` intervalos do
    # setor; a geometria de cada intervalo usa o próprio gantry)
    abertos_int, rep = [], []
    for k, arco in enumerate(arcos):
        c, angs = arco["campo"], arco["angs"]
        for s0, s1, cs in arco["setores"]:
            for j in range(s0, s1):
                d = (angs[j + 1] - angs[j] + 180.0) % 360.0 - 180.0
                abertos_int.append(Campo(f"{c.nome}^{j:03d}", float((angs[j] + d / 2) % 360.0), c.colimador, c.mesa,
                                         c.isocentro_mm, jaw_x=cs.jaw_x, jaw_y=cs.jaw_y, mlc_a=cs.mlc_a,
                                         mlc_b=cs.mlc_b))
                jr = s0 + min(((j - s0) // passo_aberto) * passo_aberto + passo_aberto // 2, s1 - 1 - s0)
                rep.append(arco["ofs_w"][jr])
    unicos = sorted(set(rep))
    am = amostrar([abertos_int[o] for o in unicos], "Calculando o campo aberto ao longo dos arcos", rapido=True)
    d0 = {o: fx * am[abertos_int[o].nome] for o in unicos}
    modelo_cp = ModeloIntervalos(arcos, mapas, abertos_int, [d0[r] for r in rep], grade, idx, sig, posicao)
    D = dose_total(Acp, Bcp, w, gg, True, "Calculando os arcos sequenciados com o motor completo")
    etapas["sequenciada_motor"] = round(float(custo(D)[0]), 5)
    # espalhamento do modelo: ajusta (fração, alcance) à dose do motor — aberturas pequenas espalham menos
    melhor = None
    for fr_esp in (0.0, 0.2, 0.4, 0.6, 0.8):
        for alc_mm in (15.0, 30.0, 60.0):
            modelo_cp.espalhamento(fr_esp, alc_mm)
            Dm = modelo_cp.avaliar(Acp, Bcp, w)
            esc = float(Dm @ D) / max(float(Dm @ Dm), 1e-30)
            err = float(np.mean((esc * Dm - D) ** 2))
            if melhor is None or err < melhor[0]:
                melhor = (err, fr_esp, alc_mm)
    modelo_cp.espalhamento(melhor[1], melhor[2])
    Dm = modelo_cp.avaliar(Acp, Bcp, w)
    etapas["modelo_erro_rel_pct"] = round(100 * float(np.sqrt(np.mean((Dm - D) ** 2)) /
                                                     max(float(np.sqrt(np.mean(D ** 2))), 1e-9)), 1)
    etapas["espalhamento"] = [melhor[1], melhor[2]]
    modelo_cp.corr = D - Dm
    bnds_w = [(lim.um_por_grau_min * dg, lim.um_por_grau_max * dg) for a in arcos for dg in a["dtheta"]]

    def otimizar_um(Acp, Bcp, w0):
        """Só as UM (lâminas paradas), no modelo calibrado: a dose é linear nas UM, então o modelo acerta bem."""
        def obj_w(x):
            f, _, _, gw, _ = modelo_cp.avaliar(Acp, Bcp, x, custo)
            return f, gw
        return I._minimizar_al(obj_w, np.clip(w0, [b[0] for b in bnds_w], [b[1] for b in bnds_w]), bnds_w, custo,
                               lambda x: modelo_cp.avaliar(Acp, Bcp, x), 4, 300)

    def doses_intervalos(Ak, Bk, rotulo):
        """Dose/UM exata de cada intervalo (motor, grade grossa, cones reduzidos): a dose é linear nas UM, então a
        otimização das UM com esta matriz é exata."""
        cs_l = []
        for k, arco in enumerate(arcos):
            c, angs = arco["campo"], arco["angs"]
            jx, jy = jaws[k]
            for j in range(len(angs) - 1):
                d = (angs[j + 1] - angs[j] + 180.0) % 360.0 - 180.0
                cs_l.append(Campo(f"{c.nome}#{j:03d}", float((angs[j] + d / 2) % 360.0), c.colimador, c.mesa,
                                  c.isocentro_mm, jaw_x=jx, jaw_y=jy,
                                  mlc_a=(0.5 * (Ak[k][j] + Ak[k][j + 1])).tolist(),
                                  mlc_b=(0.5 * (Bk[k][j] + Bk[k][j + 1])).tolist()))
        am = amostrar(cs_l, rotulo, rapido=True)
        return np.column_stack([fx * am[cs.nome] for cs in cs_l])

    def otimizar_um_exata(M, w0):
        def obj_w(x):
            f, gD = custo(M @ x)
            return f, M.T @ gD
        return I._minimizar_al(obj_w, np.clip(w0, [b[0] for b in bnds_w], [b[1] for b in bnds_w]), bnds_w, custo,
                               lambda x: M @ x, 4, 500)

    M = None
    if um_exata:
        M = doses_intervalos(Acp, Bcp, "Calculando os intervalos dos arcos com o motor completo")
        aviso("Otimizando as UM dos pontos de controle…")
        w = otimizar_um_exata(M, w)
        D = M @ w
        modelo_cp.corr[:] = 0.0
        modelo_cp.corr = D - modelo_cp.avaliar(Acp, Bcp, w)
    else:
        for r in range(2):
            aviso("Otimizando as UM dos pontos de controle…")
            w = otimizar_um(Acp, Bcp, w)
            D = dose_total(Acp, Bcp, w, gg, True, "Recalculando os arcos com as UM novas…")
            modelo_cp.corr[:] = 0.0
            modelo_cp.corr = D - modelo_cp.avaliar(Acp, Bcp, w)
    etapas["depois_das_um"] = round(float(custo(D)[0]), 5)
    hist = [float(custo(D)[0])]
    passo_mm = 10.0

    def iterar(Acp, Bcp, w, D, passo_mm, g_, rapido, rotulo):
        modelo_cp.corr[:] = 0.0
        modelo_cp.corr = D - modelo_cp.avaliar(Acp, Bcp, w)
        aviso(f"Otimizando as lâminas e as UM dos pontos de controle ({rotulo})…")
        Ak, Bk, w2 = otimizar_pontos(arcos, Acp, Bcp, w, modelo_cp, custo, colim, lim, linhas_arco, limites_x, faixas,
                                     passo_mm=passo_mm)
        for k in range(len(arcos)):
            lo, hi = limites_x[k]
            for j in range(Ak[k].shape[0]):
                a, b = Ak[k][j].copy(), Bk[k][j].copy()
                reparar(a, b, colim, lo, hi, (b - a) > colim.gap_minimo_mm + 0.5)
                Ak[k][j], Bk[k][j] = a, b
        if um_exata and g_ is gg:                          # dose exata por intervalo → UM exatas
            M2 = doses_intervalos(Ak, Bk, f"Recalculando os intervalos ({rotulo})")
            w2 = otimizar_um_exata(M2, w2)
            return Ak, Bk, w2, M2 @ w2
        D2 = dose_total(Ak, Bk, w2, g_, rapido, f"Recalculando os arcos ({rotulo})…")
        return Ak, Bk, w2, D2

    for it in range(iteracoes):
        Ak, Bk, w2, D2 = iterar(Acp, Bcp, w, D, passo_mm, gg, True, f"iteração {it + 1}")
        f2 = float(custo(D2)[0])
        aviso(f"Custo {f2:.4f} (iteração {it + 1})")
        if f2 < hist[-1]:
            Acp, Bcp, w, D = Ak, Bk, w2, D2
            hist.append(f2)
            passo_mm = min(passo_mm * 1.5, 15.0)
        else:                                                  # passo grande demais para o modelo: encolhe
            passo_mm /= 2.0
            if passo_mm < 2.0:
                break
    # polimento na grade do plano com os cones completos: o que a grade grossa não enxerga (gradientes na borda do
    # alvo); só fica se melhorar
    if polimento_fino and iteracoes > 0:
        Df = dose_total(Acp, Bcp, w, gr_fina, False, "Calculando os arcos na grade do plano…")
        f_f = float(custo(Df)[0])
        etapas["fino_antes_polimento"] = round(f_f, 5)
        Ak, Bk, w2, D2 = iterar(Acp, Bcp, w, Df, 4.0, gr_fina, False, "polimento na grade do plano")
        f2 = float(custo(D2)[0])
        etapas["fino_depois_polimento"] = round(f2, 5)
        if f2 < f_f:
            Acp, Bcp, w = Ak, Bk, w2

    # ---- final: pontos de controle nos arcos e dose na grade do plano
    restr_viol = None
    resumo = {}
    for k, arco in enumerate(arcos):
        c, angs = arco["campo"], arco["angs"]
        jx, jy = jaws[k]
        wk = w[arco["ofs_w"]]
        tot = float(wk.sum())
        c.pontos_controle = _pontos_controle(arco, Acp[k], Bcp[k], wk, jaws[k])
        c.jaw_x, c.jaw_y = (tuple(jx) if jx else None), tuple(jy)
        c.mlc_a = [round(float(x), 1) for x in Acp[k].min(axis=0)]
        c.mlc_b = [round(float(x), 1) for x in Bcp[k].max(axis=0)]
        c.peso = tot
        ruins = []
        for j, p in enumerate(c.pontos_controle):
            cs = Campo(f"{c.nome} PC{j}", p["gantry"], c.colimador, c.mesa, c.isocentro_mm, jaw_x=p["jaw_x"],
                       jaw_y=p["jaw_y"], mlc_a=p["mlc_a"], mlc_b=p["mlc_b"])
            ruins += verificar_mecanica(cs, colim)
        if ruins:
            raise ValueError(f"{c.nome}: pontos de controle com violação mecânica — {'; '.join(ruins[:3])}")
    aviso("Calculando os arcos na grade do plano…")
    for c in arcos_c:
        pronta = [d for pcs, d in cache_fino.get(c.nome, []) if _mesmos_pontos(pcs, c.pontos_controle)]
        doses_um[c.nome] = pronta[-1] if pronta else calculo.dose_por_um(grade, [c], colim, modelo, kernels, posicao,
                                                                         permitir_demonstracao)[c.nome]
    for n in [n for n in doses_um if n not in {c.nome for c in plano.campos}]:
        doses_um.pop(n)
    alvo_norm = alvos[0].mascara
    d_fx = calculo.normalizar(plano, doses_um, grade, alvo_norm)
    D = fx * d_fx.ravel()[idx]
    restr_viol = custo.violacoes(D)
    restr = {f"{o.nome} {o.tipo} {o.dose_gy:g}": round(float(v) * 100, 2) for o, v in zip(objetivos, restr_viol)
             if o.restrito}
    for n_, v in restr.items():
        if v > 1.0:
            avisos.append(f"Restrição não cumprida: {n_} (excesso {v:.1f}%) — o alvo já está no limite; afrouxe ou "
                          "mude os arcos")
    for k, arco in enumerate(arcos):
        c = arco["campo"]
        t = tempo_entrega(c, lim, linhas_arco[k])
        resumo[c.nome] = {"pontos": len(c.pontos_controle), "um": float(c.mu), **t}
        t_min = comprimento_arco(c.gantry, c.arco_fim, c.sentido) / lim.gantry_vel_max_graus_s
        if t["tempo_s"] > 2.0 * t_min:
            avisos.append(f"{c.nome}: entrega estimada em {t['tempo_s']:.0f} s (o gantry desacelera esperando as "
                          f"lâminas ou a taxa de dose em {t['intervalos_limitados_lamina']} intervalos)")
    etapas["final_fino"] = round(float(custo(D)[0]), 5)
    return ResultadoVMAT(resumo, round(sum(float(c.mu) for c in arcos_c), 1), hist, avisos,
                         round(time.time() - t0, 1), restr, etapas)
