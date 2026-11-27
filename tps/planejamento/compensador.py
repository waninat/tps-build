"""Compensador de fluência (equivalente ao "electronic compensator" do Eclipse). SRS-PLN-060..067.

Para cada campo, uma fluência F(u, v) na grade do MLC (linhas = pares de lâminas, colunas de 2,5 mm) é otimizada
em 3D: a dose de cada campo é multiplicada pela fluência ao longo do raio, e o L-BFGS-B minimiza o desvio da
prescrição em todo o núcleo do alvo, com penalidade para pontos quentes no corpo (> 107%) e suavidade entre
colunas. Depois de sequenciar em segmentos, a dose do motor completo corrige o modelo (espalhamento, penumbra,
quantização) e a otimização é refeita; fica a melhor iteração.

O sequenciamento é step-and-shoot: um segmento-base com o mínimo da fluência + `niveis` níveis iguais do excesso
(maior trecho contíguo por par). Cada segmento passa pela mesma mecânica do ajuste 3D (gap mínimo, pares fechados,
não-interdigitação do MLCi) e pela verificação mecânica; segmentos com menos de `um_min_segmento` UM são fundidos.

Pelve real (4 campos, Precise): HI 0,102 → 0,054 (método anterior, de correção pela média no raio: 0,081).
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
    formas = c.segmentos_blindagem or [{"mlc_a": c.mlc_a, "mlc_b": c.mlc_b}]   # blindagem no meio do par: união
    for i in range(colim.n_pares):
        if not (lim[i + 1] > c.jaw_y[0] and lim[i] < c.jaw_y[1]):
            continue
        for f in formas:
            if f["mlc_b"][i] - f["mlc_a"][i] > colim.gap_minimo_mm + 0.5:
                aberto[i] |= (u > f["mlc_a"][i]) & (u < f["mlc_b"][i])
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
    for n_camada, (mapa, lim_j, peso) in enumerate(camadas):
        if peso <= 0:
            continue
        base_camada = n_camada == 0
        # cada nível: os trechos de cada par; com mais de um trecho num par (pico da fluência no meio, ou blindagem
        # no meio do par) o nível vira mais de um segmento (k-ésimo trecho de cada par), cada um com o peso inteiro
        # do nível — antes só o maior trecho era entregue e o outro lado ficava sem a compensação
        trechos = {}
        for i in range(colim.n_pares):
            cols = np.nonzero(mapa[i] >= lim_j)[0]
            if len(cols) == 0:
                continue
            quebras = np.nonzero(np.diff(cols) > 1)[0]
            tr = sorted(np.split(cols, quebras + 1), key=lambda t: -float(mapa[i, t].sum()))
            if base_camada:                       # abertura (com blindagem no meio do par): todos os trechos
                tr = tr[:4]
            else:                                 # níveis: o 2º trecho só se for relevante (≥ 1,5 cm e ≥ 50% do 1º)
                m0 = float(mapa[i, tr[0]].sum())
                tr = [tr[0]] + [t for t in tr[1:3] if len(t) * res >= 15.0 and float(mapa[i, t].sum()) >= 0.5 * m0]
            tr = sorted(tr, key=lambda t: t[0])
            trechos[i] = [(max(u[t[0]] - res / 2, min(c.mlc_a[i], c.mlc_b[i])),
                           min(u[t[-1]] + res / 2, max(c.mlc_b[i], c.mlc_a[i]))) for t in tr]
        if not trechos:
            continue
        jy = c.jaw_y
        jx = c.jaw_x
        for k in range(max(len(t) for t in trechos.values())):
            a = np.full(colim.n_pares, np.nan)
            b = np.full(colim.n_pares, np.nan)
            for i, tr in trechos.items():
                if k < len(tr):
                    a[i], b[i] = tr[k]
            if np.all(np.isnan(a)):
                continue
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


def _fluencia_segmentos(segs, u, forma) -> np.ndarray:
    fq = np.zeros(forma)
    for sg in segs:
        for i_ in range(forma[0]):
            if np.isnan(sg["mlc_a"][i_]):
                continue
            fq[i_] += sg["peso"] * ((u > sg["mlc_a"][i_]) & (u < sg["mlc_b"][i_]))
    return fq


def _fundir_pequenos(segs: list[dict], mu_campo: float, um_min: float) -> list[dict]:
    """Funde segmentos com UM < mínimo ao segmento seguinte (ou anterior)."""
    segs = [dict(s) for s in segs]
    mudou = True
    while mudou and len(segs) > 1:
        mudou = False
        for k, s in enumerate(segs):
            if s["peso"] * mu_campo < um_min:
                # vai para o segmento que mais se sobrepõe (com trechos de um mesmo nível divididos, o vizinho na
                # lista pode abrir o outro lado do par); sem sobreposição, as poucas UM saem
                def sobre(o):
                    a1, b1 = np.asarray(s["mlc_a"], float), np.asarray(s["mlc_b"], float)
                    a2, b2 = np.asarray(o["mlc_a"], float), np.asarray(o["mlc_b"], float)
                    return float(np.clip(np.minimum(b1, b2) - np.maximum(a1, a2), 0, None).sum())
                outros = [j for j in range(len(segs)) if j != k]
                viz = max(outros, key=lambda j: sobre(segs[j]))
                if sobre(segs[viz]) > 0:
                    segs[viz]["peso"] += s["peso"]
                segs.pop(k)
                mudou = True
                break
    return segs


def _indices_raio(grade, mascara, c: Campo, u, v, posicao):
    """Para cada voxel da máscara: índice linear (par, coluna) do raio na grade de fluência do campo, e o peso
    de interpolação linear em u entre a coluna e a seguinte (fluência suave entre colunas)."""
    fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, posicao)
    kz, ky, kx = np.nonzero(mascara)
    pts = np.column_stack([grade.x[kx], grade.y[ky], grade.z[kz]])
    r = pts - fr.source
    w = r @ fr.w
    pu = 1000.0 * (r @ fr.u) / w
    pv = 1000.0 * (r @ fr.v) / w
    lim = np.asarray(v)                               # centros dos pares
    du = u[1] - u[0]
    fu = (pu - u[0]) / du
    j0 = np.floor(fu).astype(int)
    t = fu - j0
    dv = lim[1] - lim[0] if len(lim) > 1 else 10.0
    i = np.floor((pv - (lim[0] - dv / 2)) / dv).astype(int)
    ok = (i >= 0) & (i < len(lim)) & (j0 >= 0) & (j0 < len(u) - 1)
    return i, j0, t, ok


def compensar(plano: Plano, grade: calculo.GradeDose, doses_um: dict, alvo: np.ndarray, colim: Colimacao, modelo,
              campos: list[str] | None = None, niveis: int = 6, iteracoes: int = 3, res_u_mm: float = 2.5,
              gama: float = 0.6, limites=(0.5, 1.6), um_min_segmento: float = 2.0, kernels=None,
              posicao: str = "HFS", permitir_demonstracao: bool = False, corpo: np.ndarray | None = None,
              peso_quente: float = 1.0, suavidade: float = 0.02) -> ResultadoCompensador:
    """Compensador por otimização 3D da fluência (SRS-PLN-067).

    Modelo: a fluência F(u, v) de cada campo multiplica a dose daquele campo ao longo do raio (aproximação de
    compensador; o espalhamento lateral entra na correção iterativa). Minimiza, por L-BFGS-B com gradiente
    analítico, Σ_alvo (D/Rx − 1)² + peso_quente·Σ_corpo (D/Rx − 1,07)²₊ + suavidade·Σ (ΔF entre colunas)².
    Depois de sequenciar, a dose real (motor completo) corrige o modelo voxel a voxel e a otimização é refeita
    (`iteracoes` vezes). Fica a melhor iteração (índice de homogeneidade)."""
    from scipy.ndimage import binary_erosion
    from scipy.optimize import minimize
    plano._exigir_rascunho()
    alvos = [c for c in plano.campos if c.campo_pai is None and (campos is None or c.nome in campos)]
    if not alvos:
        raise ValueError("Nenhum campo para compensar")
    rx = plano.prescricao.dose_fracao_gy
    dose = calculo.normalizar(plano, doses_um, grade, alvo)
    hi0 = _hi(dose, alvo)
    hist = [hi0]
    nucleo = binary_erosion(alvo, iterations=1)
    if nucleo.sum() < 0.3 * alvo.sum():
        nucleo = alvo
    # voxels de controle: núcleo do alvo + corpo (pontos quentes) no alcance dos campos
    if corpo is None:
        corpo = grade.eta > 0.3
    fora = corpo & ~alvo
    mapas = {}
    f_aberto = {}
    for c in alvos:
        u, v, ab = _grade_u(c, colim, res_u_mm)
        mapas[c.nome] = (u, v, ab)
        f_aberto[c.nome] = _fluencia_segmentos(c.segmentos_blindagem, u, ab.shape) if c.segmentos_blindagem else 1.0
    abertos = {c.nome: {"mu": float(c.mu), "d": doses_um[c.nome].copy(), "jaw": (c.jaw_x, c.jaw_y),
                        "a": list(c.mlc_a), "b": list(c.mlc_b)} for c in alvos}
    fixos = [c for c in plano.campos if c not in alvos]
    idx_alvo = np.nonzero(nucleo.ravel())[0]
    corr = np.ones(len(idx_alvo))
    avisos = []
    melhor = (hi0, None, None)
    for it in range(iteracoes):
        # dose de cada campo aberto (por UM do campo aberto) nos voxels de controle
        quente = fora & (dose > 0.95 * rx)
        idx_q = np.nonzero(quente.ravel())[0]
        idx = np.r_[idx_alvo, idx_q]
        n_a = len(idx_alvo)
        cfix = sum(float(f.mu) * doses_um[f.nome].ravel()[idx] for f in fixos) if fixos else np.zeros(len(idx))
        termos, variaveis, ofs = [], [], 0
        mask_all = np.zeros(alvo.size, bool)
        mask_all[idx] = True
        mask_all = mask_all.reshape(alvo.shape)
        for c in alvos:
            u, v, ab = mapas[c.nome]
            i, j0, t, ok = _indices_raio(grade, mask_all, c, u, v, posicao)
            # ordem de np.nonzero(mask_all) == ordem crescente de índice linear; reordena para a ordem de idx
            ordem = np.argsort(np.argsort(idx))
            i, j0, t, ok = i[ordem], j0[ordem], t[ordem], ok[ordem]
            nu = len(u)
            k0 = np.where(ok, i * nu + j0, 0)
            d0 = abertos[c.nome]["mu"] * abertos[c.nome]["d"].ravel()[idx] * ok
            termos.append((ofs, k0, t, d0, nu, ab))
            variaveis.append(ab.ravel())
            ofs += ab.size
        livres = np.concatenate(variaveis)
        F0 = np.ones(ofs)
        if it > 0 and melhor[1] is not None:
            F0 = melhor_F.copy()
        fator = np.r_[corr, np.ones(len(idx_q))]

        def modelo_dose(F):
            D = cfix.copy()
            for ofs_c, k0, t, d0, nu, ab in termos:
                Fc = F[ofs_c:ofs_c + ab.size]
                D += d0 * ((1 - t) * Fc[k0] + t * Fc[np.minimum(k0 + 1, ab.size - 1)])
            return D * fator

        def obj(F):
            D = modelo_dose(F) / rx
            ra = D[:n_a] - 1.0
            rq = np.maximum(D[n_a:] - 1.07, 0.0)
            f = (ra @ ra) / n_a + peso_quente * (rq @ rq) / max(n_a, 1)
            ga = 2 * ra / n_a
            gq = 2 * peso_quente * rq / max(n_a, 1)
            gD = np.r_[ga, gq] * fator / rx
            g = np.zeros_like(F)
            for ofs_c, k0, t, d0, nu, ab in termos:
                w = gD * d0
                gc = np.zeros(ab.size)
                np.add.at(gc, k0, w * (1 - t))
                np.add.at(gc, np.minimum(k0 + 1, ab.size - 1), w * t)
                # suavidade em u (dentro de cada par)
                Fc = F[ofs_c:ofs_c + ab.size].reshape(ab.shape)
                dif = np.diff(Fc, axis=1) * (ab[:, 1:] & ab[:, :-1])
                f += suavidade * float((dif ** 2).sum()) / ab.sum()
                gs = np.zeros_like(Fc)
                gs[:, 1:] += 2 * suavidade * dif / ab.sum()
                gs[:, :-1] -= 2 * suavidade * dif / ab.sum()
                g[ofs_c:ofs_c + ab.size] = gc + gs.ravel()
            return f, g * livres

        lim_b = [(limites[0], limites[1]) if lv else (1.0, 1.0) for lv in livres]
        res = minimize(obj, F0, jac=True, method="L-BFGS-B", bounds=lim_b, options={"maxiter": 300})
        F = res.x
        for (ofs_c, k0, t, d0, nu, ab), c in zip(termos, alvos):
            u, v, _ = mapas[c.nome]
            Fc = np.where(ab, F[ofs_c:ofs_c + ab.size].reshape(ab.shape), 0.0)
            c.jaw_x, c.jaw_y = abertos[c.nome]["jaw"]
            c.mlc_a, c.mlc_b = abertos[c.nome]["a"], abertos[c.nome]["b"]
            c.segmentos = sequenciar(c, colim, u, Fc, ab, niveis)
            mapas[c.nome] = (u, v, ab)
        # fluência efetivamente entregue (depois do sequenciamento), na escala de UM nova de cada campo
        Fq = np.ones_like(F)
        doses_um.update(calculo.dose_por_um(grade, alvos, colim, modelo, kernels, posicao, permitir_demonstracao))
        dose = calculo.normalizar(plano, doses_um, grade, alvo)
        hist.append(_hi(dose, alvo))
        for (ofs_c, k0, t, d0, nu, ab), c in zip(termos, alvos):
            u = mapas[c.nome][0]
            fq = _fluencia_segmentos(c.segmentos, u, ab.shape)
            fa = f_aberto[c.nome]
            fq = np.where(np.asarray(fa) > 0, fq / np.maximum(fa, 1e-9), 0.0) if np.ndim(fa) else fq
            Fq[ofs_c:ofs_c + ab.size] = (fq * float(c.mu) / max(abertos[c.nome]["mu"], 1e-9)).ravel()
        modelo_alvo = modelo_dose(Fq)[:n_a] / np.maximum(fator[:n_a], 1e-9)
        if hist[-1] < melhor[0]:
            melhor = (hist[-1], {c.nome: [dict(x) for x in c.segmentos] for c in alvos},
                      {c.nome: doses_um[c.nome] for c in alvos})
            melhor_F = F.copy()
        # correção do modelo: razão dose real / dose do modelo no alvo (espalhamento, penumbra, quantização)
        real = dose.ravel()[idx_alvo]
        razao = real / np.maximum(modelo_alvo, 1e-9)
        corr = np.clip(razao / np.median(razao), 0.8, 1.2)
    if melhor[1] is None:                              # nada a compensar: volta aos campos abertos
        for c in alvos:
            c.segmentos = [dict(x) for x in c.segmentos_blindagem] if c.segmentos_blindagem else None
        doses_um.update(calculo.dose_por_um(grade, alvos, colim, modelo, kernels, posicao, permitir_demonstracao))
        calculo.normalizar(plano, doses_um, grade, alvo)
        return ResultadoCompensador({}, {c.nome: 0 for c in alvos}, hi0, hi0, hist,
                                    ["Compensador sem ganho: campos mantidos abertos"])
    for c in alvos:                                    # fica a melhor iteração
        c.segmentos = melhor[1][c.nome]
        doses_um[c.nome] = melhor[2][c.nome]
    dose = calculo.normalizar(plano, doses_um, grade, alvo)
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
    return ResultadoCompensador({n: (m[0], None) for n, m in mapas.items()},
                                {c.nome: len(c.segmentos) for c in alvos}, hi0, _hi(dose, alvo), hist, avisos)
