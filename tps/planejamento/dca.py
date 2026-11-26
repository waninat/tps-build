"""Arco conformacional dinâmico (DCA) — arco 3D, sem modulação de intensidade. SRS-PLN-110.

Em cada ponto de controle (a cada `passo` graus) as lâminas se conformam ao alvo + margem no BEV daquele ângulo;
as UM por grau são constantes (pesos acumulados lineares no ângulo). Mandíbulas fixas no envelope do arco (como
o VMAT do Elekta). Opcional: a abertura recua sobre um órgão de risco no BEV, mas continua uma abertura única por
par (sem buraco, sem varredura) — o plano continua "3D".

A dose sai pelo mesmo caminho do VMAT (intervalos entre pontos de controle), a exportação é um feixe DYNAMIC por
arco, e o tempo de entrega/colisão/QA reaproveitam o VMAT.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

import numpy as np

from tps.planejamento.conformacao import Colimacao, conformar, pontos_do_alvo, projetar_bev
from tps.planejamento.modelo import Campo
from tps.planejamento.vmat import angulos_cp, reparar

COLIMADORES_CANDIDATOS = (0.0, 15.0, 30.0, 45.0, 60.0, 75.0, 90.0, 285.0, 300.0, 315.0, 330.0, 345.0)


@dataclass
class InfoDCA:
    n_pontos: int = 0
    deslocamento_max_mm_grau: float = 0.0
    area_fora_cm2: float = 0.0           # média, abertura além do alvo projetado (orelhas das lâminas)
    bloqueio_pct: float = 0.0            # média, % do alvo projetado coberto por bloqueio de órgão
    avisos: list = field(default_factory=list)


def _oar_intervalos(pts_u, pts_v, lim, margem):
    """Para cada par: intervalo [u0, u1] que o órgão ocupa (com margem) ou None."""
    out = []
    for i in range(len(lim) - 1):
        sel = (pts_v >= lim[i] - margem) & (pts_v <= lim[i + 1] + margem)
        out.append((pts_u[sel].min() - margem, pts_u[sel].max() + margem) if sel.any() else None)
    return out


def _bloquear(a, b, oar_iv, alvo_iv, g, max_bloqueio_pct):
    """Recua a lâmina do lado em que o órgão está (sem abrir buraco). Não bloqueia mais que max_bloqueio_pct da
    extensão do alvo naquele par."""
    for i, iv in enumerate(oar_iv):
        if iv is None or b[i] - a[i] <= g + 0.5:
            continue
        o0, o1 = iv
        if o1 <= a[i] or o0 >= b[i]:
            continue
        ext = alvo_iv[i]
        largura = (ext[1] - ext[0]) if ext else (b[i] - a[i])
        lim = max_bloqueio_pct / 100.0 * largura
        if o0 <= a[i] + 1e-6 and o1 < b[i]:              # órgão do lado A: A avança
            a[i] = min(o1, a[i] + lim, b[i] - g)
        elif o1 >= b[i] - 1e-6 and o0 > a[i]:            # órgão do lado B: B recua
            b[i] = max(o0, b[i] - lim, a[i] + g)
        # órgão no meio do intervalo: não bloqueia (seria preciso abrir buraco = modulação)


def gerar_arco(c: Campo, alvo: np.ndarray, eixos: tuple, colim: Colimacao, margem_mm: float, passo: float = 5.0,
               posicao: str = "HFS", sad_mm: float = 1000.0, oars: list | None = None,
               max_bloqueio_pct: float = 50.0) -> InfoDCA:
    """Preenche c.pontos_controle (o campo já tem gantry, arco_fim, sentido, colimador e mesa). oars: [(máscara,
    margem_mm)]."""
    x, y, z = eixos
    pts = pontos_do_alvo(alvo, x, y, z)
    pts_oar = [(pontos_do_alvo(m, x, y, z), float(mg)) for m, mg in (oars or []) if m is not None and m.any()]
    angs = angulos_cp(float(c.gantry), float(c.arco_fim), c.sentido, passo)
    lim = np.asarray(colim.limites_laminas, float)
    g = colim.gap_minimo_mm
    A, B, abertos, fora, bloq = [], [], [], [], []
    info = InfoDCA(n_pontos=len(angs))
    for ang in angs:
        t = copy.deepcopy(c)
        t.gantry, t.arco_fim, t.pontos_controle = float(ang), None, None
        conformar(t, alvo, eixos, colim, margem_mm, sad_mm, posicao)
        a, b = np.asarray(t.mlc_a, float), np.asarray(t.mlc_b, float)
        u, v = projetar_bev(pts, t, sad_mm, posicao)
        alvo_iv = _oar_intervalos(u, v, lim, 0.0)
        ab = np.array([iv is not None for iv in _oar_intervalos(u, v, lim, margem_mm)])
        larg0 = sum((iv[1] - iv[0]) * (lim[i + 1] - lim[i]) for i, iv in enumerate(alvo_iv) if iv)
        for po, mg in pts_oar:
            ou, ov = projetar_bev(po, t, sad_mm, posicao)
            _bloquear(a, b, _oar_intervalos(ou, ov, lim, mg), alvo_iv, g, max_bloqueio_pct)
        coberto = 0.0
        for i, iv in enumerate(alvo_iv):
            if iv:
                coberto += max(0.0, min(iv[1], b[i]) - max(iv[0], a[i])) * (lim[i + 1] - lim[i])
        bloq.append(100.0 * (1 - coberto / larg0) if larg0 > 0 else 0.0)
        area = float(np.sum(np.clip(b - a, 0, None)[ab] * np.diff(lim)[ab]))
        fora.append(max(0.0, area - larg0) / 100.0)
        A.append(a)
        B.append(b)
        abertos.append(ab)
    A, B, AB = np.array(A), np.array(B), np.array(abertos)
    # mandíbulas fixas: envelope do arco
    # Y: o envelope do alvo + margem em todos os ângulos (não a borda da lâmina)
    vmin, vmax, umin, umax = np.inf, -np.inf, np.inf, -np.inf
    for ang in angs:
        t = copy.deepcopy(c)
        t.gantry, t.arco_fim = float(ang), None
        u, v = projetar_bev(pts, t, sad_mm, posicao)
        vmin, vmax = min(vmin, v.min() - margem_mm), max(vmax, v.max() + margem_mm)
        umin, umax = min(umin, u.min() - margem_mm), max(umax, u.max() + margem_mm)
    jy = (round(float(max(vmin, -colim.jaw_y_max_mm)), 1), round(float(min(vmax, colim.jaw_y_max_mm)), 1))
    jx = (round(float(umin) - 2.0, 1), round(float(umax) + 2.0, 1)) if colim.tem_jaw_x else None
    # pares fechados: gap parado no centro do envelope (não "passeia" de um ponto para o outro)
    # pares fechados: o gap fica onde a abertura do próprio par estava no ponto aberto mais próximo (a lâmina
    # fecha no meio da abertura, sem atravessar o campo); par nunca aberto: centro do envelope
    centro = 0.5 * (umin + umax)
    n_cp = len(angs)
    for i in range(A.shape[1]):
        abertos_i = np.nonzero(AB[:, i])[0]
        for k in np.nonzero(~AB[:, i])[0]:
            if abertos_i.size:
                j = abertos_i[np.argmin(np.abs(abertos_i - k))]
                ci = 0.5 * (A[j, i] + B[j, i])
            else:
                ci = centro
            A[k, i], B[k, i] = ci - g / 2, ci + g / 2
    for k in range(n_cp):
        reparar(A[k], B[k], colim, -colim.lamina_max_mm, colim.lamina_max_mm, AB[k])
    # pesos acumulados lineares no ângulo: UM por grau constante
    L = np.abs((np.diff(angs) + 180.0) % 360.0 - 180.0)
    mw = np.r_[0.0, np.cumsum(L) / L.sum()]
    mw[-1] = 1.0
    c.pontos_controle = [{"gantry": round(float(angs[k]), 2), "mlc_a": [round(float(q), 1) for q in A[k]],
                          "mlc_b": [round(float(q), 1) for q in B[k]], "jaw_x": list(jx) if jx else None,
                          "jaw_y": list(jy), "mw": round(float(mw[k]), 6)} for k in range(len(angs))]
    c.jaw_x, c.jaw_y = jx, jy
    c.mlc_a = [round(float(q), 1) for q in A.min(axis=0)]
    c.mlc_b = [round(float(q), 1) for q in B.max(axis=0)]
    c.margem_mm = float(margem_mm)
    desl = np.maximum(np.abs(np.diff(A, axis=0)), np.abs(np.diff(B, axis=0))).max(axis=1) / np.maximum(L, 1e-6)
    info.deslocamento_max_mm_grau = float(desl.max()) if desl.size else 0.0
    info.area_fora_cm2 = float(np.mean(fora))
    info.bloqueio_pct = float(np.mean(bloq))
    return info


def melhor_colimador(c: Campo, alvo: np.ndarray, eixos: tuple, colim: Colimacao, margem_mm: float,
                     passo: float = 10.0, posicao: str = "HFS", candidatos=COLIMADORES_CANDIDATOS) -> tuple[float, dict]:
    """Ângulo de colimador que deixa menos abertura além do alvo ao longo do arco (orelhas das lâminas de 1 cm)."""
    custo = {}
    for col in candidatos:
        t = copy.deepcopy(c)
        t.colimador = float(col)
        try:
            inf = gerar_arco(t, alvo, eixos, colim, margem_mm, passo, posicao)
        except ValueError:
            continue
        custo[col] = inf.area_fora_cm2 + 0.02 * inf.deslocamento_max_mm_grau
    if not custo:
        raise ValueError("Alvo + margem não cabe no campo em nenhum ângulo de colimador")
    return min(custo, key=custo.get), custo
