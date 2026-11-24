"""IMRT step-and-shoot com campos fixos (Elekta Precise/MLCi e Synergy/Agility). SRS-IMR-001..020.

Três etapas, como nos sistemas comerciais (otimização de fluência → sequenciamento → otimização dos segmentos):

1. **Fluência.** Cada campo tem um mapa F(par, coluna) — linhas = pares de lâminas, colunas de 2,5 mm — dentro da
   abertura do campo conformado ao(s) alvo(s). Modelo rápido de dose: dose do campo aberto (motor completo, por UM)
   × F borrada por um núcleo gaussiano no BEV (penumbra/espalhamento lateral) ao longo do raio, + uma correção
   aditiva voxel a voxel vinda da dose real (etapa 3) — o "dose correction loop". L-BFGS-B com gradiente analítico.
2. **Sequenciamento** (sweep unidirecional, Bortfeld 1994): a fluência é quantizada em `niveis` e cada par vira
   intervalos [esquerda, direita) que entregam o perfil exato; os segmentos são montados par a par respeitando a
   mecânica (gap mínimo e, no MLCi, sem interdigitação: o intervalo mais à direita espera o próximo segmento).
3. **Segmentos.** A dose de cada segmento é calculada pelo motor completo (CCC) e os pesos (UM) de todos os
   segmentos são otimizados juntos com a dose exata; segmentos abaixo da UM ou área mínimas saem e os pesos são
   refeitos. A diferença entre a dose real e o modelo rápido volta para a etapa 1 (`iteracoes`).

Objetivos (mesmos da tela): alvo (dose uniforme, sub e sobredosagem), máximo, média e ponto de DVH para órgãos,
e o "tecido normal" automático (dose fora do alvo caindo com a distância, como o NTO do Eclipse).
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import binary_dilation, distance_transform_edt, gaussian_filter
from scipy.optimize import minimize

from tps.engine.geometry import beam_frame
from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import OpcoesAjuste, _posicionar_fechados, verificar_mecanica
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano


# ------------------------------------------------------------------ objetivos
@dataclass
class Objetivo:
    nome: str
    mascara: np.ndarray
    tipo: str                         # alvo | max | media | dvh | min
    dose_gy: float                    # dose total (Gy)
    volume_pct: float | None = None   # dvh: no máximo volume_pct% do órgão acima de dose_gy
    peso: float = 1.0


@dataclass
class ResultadoIMRT:
    n_segmentos: dict
    um_total: float
    custo: list = field(default_factory=list)
    metricas: dict = field(default_factory=dict)
    avisos: list = field(default_factory=list)
    tempo_s: float = 0.0


class Custo:
    """Função custo sobre as doses (Gy, dose total) nos voxels amostrados, com gradiente por voxel."""

    def __init__(self, objetivos: list[Objetivo], idx_por_obj: list[np.ndarray], nto: tuple | None):
        self.obj = objetivos
        self.idx = idx_por_obj
        self.nto = nto                # (índices, limite por voxel em Gy, peso)

    def __call__(self, D: np.ndarray):
        f = 0.0
        g = np.zeros_like(D)
        for o, ii in zip(self.obj, self.idx):
            if len(ii) == 0:
                continue
            d = D[ii]
            n = len(ii)
            p = o.peso
            if o.tipo == "alvo":
                r = d / o.dose_gy - 1.0
                sub = np.minimum(d / o.dose_gy - 0.95, 0.0)
                sob = np.maximum(d / o.dose_gy - 1.07, 0.0)
                f += p * (r @ r + 50 * sub @ sub + 50 * sob @ sob) / n
                np.add.at(g, ii, p * 2 * (r + 50 * sub + 50 * sob) / (n * o.dose_gy))
            elif o.tipo == "min":
                sub = np.minimum(d / o.dose_gy - 1.0, 0.0)
                f += 50 * p * (sub @ sub) / n
                np.add.at(g, ii, 100 * p * sub / (n * o.dose_gy))
            elif o.tipo == "max":
                e = np.maximum(d / o.dose_gy - 1.0, 0.0)
                f += 20 * p * (e @ e) / n
                np.add.at(g, ii, 40 * p * e / (n * o.dose_gy))
            elif o.tipo == "media":
                m = d.mean()
                e = max(m / o.dose_gy - 1.0, 0.0)
                f += 20 * p * e * e
                np.add.at(g, ii, 40 * p * e / (o.dose_gy * n))
            elif o.tipo == "dvh":
                k = int(np.floor(n * (o.volume_pct or 0) / 100.0))
                ordem = np.argsort(d)[::-1]
                sel = ordem[k:]                                    # os que precisam ficar abaixo do nível
                e = np.maximum(d[sel] / o.dose_gy - 1.0, 0.0)
                if len(sel):
                    f += 20 * p * (e @ e) / len(sel)
                    np.add.at(g, ii[sel], 40 * p * e / (len(sel) * o.dose_gy))
        if self.nto is not None:
            ii, lim, p = self.nto
            e = np.maximum(D[ii] / lim - 1.0, 0.0)
            f += 10 * p * (e @ e) / len(ii)
            np.add.at(g, ii, 20 * p * e / (len(ii) * lim))
        return f, g


# ------------------------------------------------------------------ geometria da fluência
def _grade_fluencia(c: Campo, colim: Colimacao, res: float):
    jx = c.jaw_x if c.jaw_x else (min(c.mlc_a), max(c.mlc_b))
    u = np.arange(np.floor(jx[0] / res) * res + res / 2, jx[1], res)
    lim = np.asarray(colim.limites_laminas)
    v = 0.5 * (lim[1:] + lim[:-1])
    ab = np.zeros((colim.n_pares, len(u)), bool)
    for i in range(colim.n_pares):
        if lim[i + 1] > c.jaw_y[0] + 1 and lim[i] < c.jaw_y[1] - 1 and c.mlc_b[i] - c.mlc_a[i] > colim.gap_minimo_mm + 0.5:
            ab[i] = (u > c.mlc_a[i]) & (u < c.mlc_b[i])
    return u, v, ab


def _raios(grade, idx_lin: np.ndarray, c: Campo, u, v, posicao):
    """Para cada voxel (índice linear): par i, coluna j0 e peso t de interpolação linear em u."""
    kz, ky, kx = np.unravel_index(idx_lin, grade.eta.shape)
    pts = np.column_stack([grade.x[kx], grade.y[ky], grade.z[kz]])
    fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, posicao)
    r = pts - fr.source
    w = r @ fr.w
    pu = 1000.0 * (r @ fr.u) / w
    pv = 1000.0 * (r @ fr.v) / w
    du = u[1] - u[0]
    fu = (pu - u[0]) / du
    j0 = np.floor(fu).astype(int)
    t = fu - j0
    dv = v[1] - v[0]
    i = np.floor((pv - (v[0] - dv / 2)) / dv).astype(int)
    ok = (i >= 0) & (i < len(v)) & (j0 >= 0) & (j0 < len(u) - 1)
    return np.where(ok, i, 0), np.where(ok, j0, 0), t, ok


class ModeloFluencia:
    """D = Σ_campos d_aberto · interp(G ∗ F_estendida) + correção. F livre só dentro da abertura do campo."""

    def __init__(self, campos, mapas, grade, idx, d_aberto, sigma_px, posicao):
        self.campos = campos
        self.blocos = []
        ofs = 0
        for c in campos:
            u, v, ab = mapas[c.nome]
            i, j0, t, ok = _raios(grade, idx, c, u, v, posicao)
            nu = len(u)
            # fora da abertura: valor do vizinho mais próximo de dentro (sem dupla penumbra na borda)
            _, (ii, jj) = distance_transform_edt(~ab, return_indices=True)
            perto = (ii * nu + jj).ravel()
            self.blocos.append({"ofs": ofs, "n": ab.size, "shape": ab.shape, "ab": ab, "k0": i * nu + j0, "t": t,
                                "d0": d_aberto[c.nome] * ok, "perto": perto})
            ofs += ab.size
        self.n = ofs
        self.sigma = sigma_px
        self.corr = np.zeros(len(idx))

    def livres(self):
        return np.concatenate([b["ab"].ravel() for b in self.blocos])

    def dose(self, F):
        D = self.corr.copy()
        for b in self.blocos:
            Fc = F[b["ofs"]:b["ofs"] + b["n"]][b["perto"]].reshape(b["shape"])
            Fe = gaussian_filter(Fc, self.sigma, mode="nearest").ravel()
            k0, t = b["k0"], b["t"]
            D += b["d0"] * ((1 - t) * Fe[k0] + t * Fe[np.minimum(k0 + 1, b["n"] - 1)])
        return D

    def grad(self, gD):
        g = np.zeros(self.n)
        for b in self.blocos:
            w = gD * b["d0"]
            ge = np.zeros(b["n"])
            np.add.at(ge, b["k0"], w * (1 - b["t"]))
            np.add.at(ge, np.minimum(b["k0"] + 1, b["n"] - 1), w * b["t"])
            ge = gaussian_filter(ge.reshape(b["shape"]), self.sigma, mode="nearest").ravel()   # adjunto (simétrico)
            gc = np.zeros(b["n"])
            np.add.at(gc, b["perto"], ge)
            g[b["ofs"]:b["ofs"] + b["n"]] = gc
        return g


# ------------------------------------------------------------------ sequenciamento
def intervalos_sweep(perfil: np.ndarray) -> list[tuple[int, int]]:
    """Perfil inteiro de um par → intervalos [j_esq, j_dir) cuja soma é exatamente o perfil (sweep unidirecional,
    mínimo de UM; Bortfeld et al. 1994)."""
    p = np.r_[0, np.asarray(perfil, int), 0]
    d = np.diff(p)
    esq = np.repeat(np.arange(len(d)), np.maximum(d, 0))
    dirs = np.repeat(np.arange(len(d)), np.maximum(-d, 0))
    return list(zip(esq.tolist(), dirs.tolist()))


def sequenciar_sweep(c: Campo, colim: Colimacao, u: np.ndarray, Fq: np.ndarray, ab: np.ndarray) -> list[dict]:
    """Fluência inteira (níveis) → segmentos com peso 1 cada (unidades de nível), mecânica conferida."""
    res = u[1] - u[0]
    g = colim.gap_minimo_mm
    filas = [intervalos_sweep(np.where(ab[i], Fq[i], 0)) for i in range(Fq.shape[0])]
    interdig = getattr(colim, "interdigitacao", True)
    segs = []
    op = OpcoesAjuste()
    guarda = 0
    while any(filas) and guarda < 2000:
        guarda += 1
        a = np.full(colim.n_pares, np.nan)
        b = np.full(colim.n_pares, np.nan)
        usados = []
        # do intervalo mais à esquerda para a direita: entra se não conflitar com vizinhos já no segmento
        cand = sorted((filas[i][0][0], i) for i in range(len(filas)) if filas[i])
        for _, i in cand:
            je, jd = filas[i][0]
            ai = max(u[je] - res / 2, c.mlc_a[i])
            bi = min(u[jd - 1] + res / 2, c.mlc_b[i])
            if bi - ai < g:
                ci = 0.5 * (ai + bi)
                ai, bi = ci - g / 2, ci + g / 2
            if not interdig:
                if any(0 <= j < len(a) and not np.isnan(a[j]) and (ai > b[j] - g or a[j] > bi - g) for j in (i - 1, i + 1)):
                    continue
            a[i], b[i] = ai, bi
            usados.append(i)
        for i in usados:
            filas[i].pop(0)
        abertos = ~np.isnan(a)
        lim = np.asarray(colim.limites_laminas)
        rows = np.nonzero(abertos)[0]
        jy = [max(c.jaw_y[0], float(lim[rows.min()])), min(c.jaw_y[1], float(lim[rows.max() + 1]))]
        jx = None
        if c.jaw_x:
            jx = [max(c.jaw_x[0], float(np.nanmin(a)) - 2.0), min(c.jaw_x[1], float(np.nanmax(b)) + 2.0)]
        _posicionar_fechados(a, b, colim, jx, op)
        s = Campo(c.nome, c.gantry, c.colimador, c.mesa, c.isocentro_mm, jaw_x=jx, jaw_y=jy,
                  mlc_a=[round(float(x), 1) for x in a], mlc_b=[round(float(x), 1) for x in b])
        erros = verificar_mecanica(s, colim)
        if erros:
            raise ValueError(f"{c.nome}: segmento inválido: {'; '.join(erros)}")
        chave = (tuple(s.mlc_a), tuple(s.mlc_b))
        igual = next((q for q in segs if (tuple(q["mlc_a"]), tuple(q["mlc_b"])) == chave), None)
        if igual:
            igual["peso"] += 1.0
        else:
            segs.append({"mlc_a": s.mlc_a, "mlc_b": s.mlc_b, "jaw_x": jx, "jaw_y": jy, "peso": 1.0})
    return segs


def _area_cm2(s: dict, colim: Colimacao) -> float:
    lim = np.asarray(colim.limites_laminas)
    a = 0.0
    for i in range(colim.n_pares):
        lo, hi = max(lim[i], s["jaw_y"][0]), min(lim[i + 1], s["jaw_y"][1])
        if hi > lo:
            a += max(s["mlc_b"][i] - s["mlc_a"][i], 0.0) * (hi - lo)
    return a / 100.0


# ------------------------------------------------------------------ otimização
def _amostras(mascaras: list[np.ndarray], n_max: list[int], rng) -> tuple[np.ndarray, list[np.ndarray]]:
    sel = []
    for m, n in zip(mascaras, n_max):
        k = np.flatnonzero(m)
        if len(k) > n:
            k = np.sort(rng.choice(k, n, replace=False))
        sel.append(k)
    todos = np.unique(np.concatenate(sel)) if sel else np.zeros(0, int)
    return todos, [np.searchsorted(todos, k) for k in sel]


def otimizar_imrt(plano: Plano, grade: calculo.GradeDose, doses_um: dict, objetivos: list[Objetivo],
                  corpo: np.ndarray, colim: Colimacao, modelo, niveis: int = 7, iteracoes: int = 2,
                  res_u_mm: float = 2.5, sigma_mm: tuple = (3.0, 3.5), um_min_segmento: float = 3.0,
                  area_min_cm2: float = 4.0, peso_nto: float = 1.0, suavidade: float = 0.05, kernels=None,
                  posicao: str = "HFS", permitir_demonstracao: bool = False, progresso=None) -> ResultadoIMRT:
    """Otimiza e segmenta todos os campos principais do plano (que já devem estar conformados aos alvos)."""
    t0 = time.time()
    plano._exigir_rascunho()
    aviso = progresso or (lambda m: None)
    campos = [c for c in plano.campos if c.campo_pai is None]
    if not campos:
        raise ValueError("Crie os campos (ângulos) antes de otimizar")
    alvos = [o for o in objetivos if o.tipo == "alvo"]
    if not alvos:
        raise ValueError("Defina pelo menos um alvo com dose")
    fx = plano.prescricao.fracoes
    rng = np.random.default_rng(0)
    for c in campos:                                     # parte dos campos abertos conformados
        c.segmentos = None
    faltam = [c for c in campos if c.nome not in doses_um]
    if faltam:
        doses_um.update(calculo.dose_por_um(grade, faltam, colim, modelo, kernels, posicao, permitir_demonstracao))
    # tecido normal: corpo fora dos alvos (+3 mm), limite caindo com a distância (NTO)
    passo = float(abs(grade.x[1] - grade.x[0]))
    todos_alvos = np.zeros_like(corpo)
    for o in alvos:
        todos_alvos |= o.mascara
    dist = distance_transform_edt(~todos_alvos, sampling=(abs(grade.z[1] - grade.z[0]), abs(grade.y[1] - grade.y[0]),
                                                           passo))
    rx_max = max(o.dose_gy for o in alvos)
    normal = corpo & (dist > 3.0)
    # amostras
    mascs = [o.mascara for o in objetivos] + [normal]
    nmax = [15000 if o.tipo == "alvo" else 6000 for o in objetivos] + [20000]
    idx, partes = _amostras(mascs, nmax, rng)
    lim_nto = rx_max * (0.55 + 0.50 * np.exp(-0.05 * (dist.ravel()[idx[partes[-1]]] - 3.0)))
    custo = Custo(objetivos, partes[:-1], (partes[-1], lim_nto, peso_nto) if peso_nto > 0 else None)
    mapas = {c.nome: _grade_fluencia(c, colim, res_u_mm) for c in campos}
    mu0 = {c.nome: float(c.mu or 100.0) for c in campos}
    d_ab = {c.nome: fx * mu0[c.nome] * doses_um[c.nome].ravel()[idx] for c in campos}
    lim = np.asarray(colim.limites_laminas)
    sig = (sigma_mm[0] / float(lim[1] - lim[0]), sigma_mm[1] / res_u_mm)
    mod = ModeloFluencia(campos, mapas, grade, idx, d_ab, sig, posicao)
    livres = mod.livres()
    F = np.where(livres, 1.0, 0.0)
    # escala inicial: alvo principal na média
    D0 = mod.dose(F)
    a0 = alvos[0]
    F *= a0.dose_gy / max(float(D0[partes[objetivos.index(a0)]].mean()), 1e-9)
    hist = []
    melhor = None
    abertos_orig = {c.nome: (list(c.mlc_a), list(c.mlc_b), c.jaw_x, c.jaw_y) for c in campos}
    ver = {}

    def suav(Fv):
        f, g = 0.0, np.zeros_like(Fv)
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
            g[bl["ofs"]:bl["ofs"] + bl["n"]] = gs.ravel()
        return f, g

    for it in range(iteracoes + 1):
        # ---- 1. fluência
        aviso(f"Otimizando a fluência (iteração {it + 1})…")

        def obj(Fv):
            D = mod.dose(Fv)
            f, gD = custo(D)
            fs, gs = suav(Fv)
            return f + fs, (mod.grad(gD) + gs) * livres

        r = minimize(obj, F, jac=True, method="L-BFGS-B", bounds=[(0.0, None)] * len(F),
                     options={"maxiter": 400, "ftol": 1e-13, "gtol": 1e-11})
        aviso(f"Fluência: custo {r.fun:.5f} ({r.nit} iterações)")
        F = r.x
        # ---- 2. sequenciamento
        aviso("Sequenciando os segmentos…")
        segs_por_campo = {}
        for bl, c in zip(mod.blocos, campos):
            u, v, ab = mapas[c.nome]
            Fc = np.where(ab, F[bl["ofs"]:bl["ofs"] + bl["n"]].reshape(bl["shape"]), 0.0)
            fmax = float(Fc.max())
            if fmax <= 1e-6:
                segs_por_campo[c.nome] = ([], 0.0)
                continue
            delta = fmax / niveis
            Fq = np.rint(Fc / delta).astype(int)
            c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y = copy.deepcopy(abertos_orig[c.nome])
            segs_por_campo[c.nome] = (sequenciar_sweep(c, colim, u, Fq, ab), delta)
        # ---- 3. dose de cada segmento (motor completo) e pesos dos segmentos
        lista = []
        for c in campos:
            segs, delta = segs_por_campo[c.nome]
            for k, s in enumerate(segs):
                chave = (c.nome, tuple(s["mlc_a"]), tuple(s["mlc_b"]), tuple(s["jaw_y"]))
                lista.append((c, k, s, delta, chave))
        novos = [(c, s, ch) for c, k, s, d, ch in lista if ch not in ver]
        aviso(f"Calculando {len(novos)} segmento(s) com o motor completo…")
        if novos:
            tmp = []
            for j, (c, s, ch) in enumerate(novos):
                cs = Campo(f"{c.nome}#s{j}", c.gantry, c.colimador, c.mesa, c.isocentro_mm, jaw_x=s["jaw_x"],
                           jaw_y=s["jaw_y"], mlc_a=s["mlc_a"], mlc_b=s["mlc_b"])
                tmp.append((cs, ch))
            doses = calculo.dose_por_um(grade, [t[0] for t in tmp], colim, modelo, kernels, posicao,
                                        permitir_demonstracao)
            for cs, ch in tmp:
                ver[ch] = doses[cs.nome].astype(np.float32)
        A = np.column_stack([fx * ver[ch].ravel()[idx] for _, _, _, _, ch in lista])   # Gy por UM (total)
        # pesos iniciais: unidades de nível × escala da fluência (UM do campo aberto × delta)
        w0 = np.array([s["peso"] * d * mu0[c.nome] for c, k, s, d, ch in lista])
        ativo = np.ones(len(lista), bool)
        for _ in range(3):
            def obj_w(w):
                D = A @ w
                f, gD = custo(D)
                return f, A.T @ gD
            bnds = [(0.0, None) if a_ else (0.0, 0.0) for a_ in ativo]
            rw = minimize(obj_w, np.where(ativo, w0, 0.0), jac=True, method="L-BFGS-B", bounds=bnds,
                          options={"maxiter": 500, "ftol": 1e-13, "gtol": 1e-11})
            w = rw.x
            fracos = ativo & ((w < um_min_segmento) | np.array([_area_cm2(s, colim) < area_min_cm2
                                                               for _, _, s, _, _ in lista]))
            if not fracos.any():
                break
            ativo &= ~fracos
            w0 = w
        w = np.where(ativo, w, 0.0)
        fval = float(custo(A @ w)[0])
        hist.append(fval)
        aviso(f"Custo {fval:.4f} ({int(ativo.sum())} segmentos)")
        if melhor is None or fval < melhor[0]:
            melhor = (fval, [(c.nome, s, float(wi)) for (c, k, s, d, ch), wi in zip(lista, w) if wi > 0],
                      {ch: wi for (c, k, s, d, ch), wi in zip(lista, w) if wi > 0})
        if it == iteracoes:
            break
        # correção do modelo rápido: dose real − modelo com a fluência entregue
        Freal = np.zeros_like(F)
        for bl, c in zip(mod.blocos, campos):
            u, v, ab = mapas[c.nome]
            fq = np.zeros(bl["shape"])
            for (cc, k, s, d, ch), wi in zip(lista, w):
                if cc is not c or wi <= 0:
                    continue
                for i_ in range(bl["shape"][0]):
                    fq[i_] += wi / mu0[c.nome] * ((u > s["mlc_a"][i_]) & (u < s["mlc_b"][i_]) &
                                                  (v[i_] > s["jaw_y"][0]) & (v[i_] < s["jaw_y"][1]))
            Freal[bl["ofs"]:bl["ofs"] + bl["n"]] = fq.ravel()
        mod.corr[:] = 0.0
        mod.corr = (A @ w) - mod.dose(Freal)
    # ---- resultado: segmentos nos campos
    _, segs_fin, _ = melhor
    avisos = []
    for c in campos:
        meus = [(s, wi) for n, s, wi in segs_fin if n == c.nome]
        c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y = copy.deepcopy(abertos_orig[c.nome])
        if not meus:
            avisos.append(f"{c.nome}: nenhum segmento útil (campo sem peso)")
            c.segmentos = None
            c.peso = 1e-4
            continue
        tot = sum(wi for _, wi in meus)
        c.segmentos = [{"mlc_a": s["mlc_a"], "mlc_b": s["mlc_b"], "jaw_x": s["jaw_x"], "jaw_y": s["jaw_y"],
                        "peso": wi / tot} for s, wi in meus]
        c.peso = tot
        c.mlc_a = [min(s["mlc_a"][i] for s, _ in meus) for i in range(colim.n_pares)]
        c.mlc_b = [max(s["mlc_b"][i] for s, _ in meus) for i in range(colim.n_pares)]
        doses_um[c.nome] = sum((wi / tot) * ver[(c.nome, tuple(s["mlc_a"]), tuple(s["mlc_b"]), tuple(s["jaw_y"]))]
                               for s, wi in meus)
    sem = [c for c in campos if c.segmentos is None]
    for c in sem:
        plano.campos.remove(c)
        doses_um.pop(c.nome, None)
    calculo.normalizar(plano, doses_um, grade, alvos[0].mascara)
    return ResultadoIMRT({c.nome: len(c.segmentos) for c in plano.campos if c.segmentos},
                         round(sum(float(c.mu or 0) for c in plano.campos), 1), hist, {}, avisos,
                         round(time.time() - t0, 1))


def campos_equiespacados(n: int, inicio: float = 0.0, evitar: tuple | None = None) -> list[float]:
    """Ângulos de gantry igualmente espaçados (ex.: 5 → 0, 72, 144, 216, 288; 7 → 0, 51, 103…)."""
    return [round((inicio + k * 360.0 / n) % 360, 1) for k in range(n)]


def objetivos_sugeridos(nomes: list[str], alvo: str | None, dose_alvo_gy: float, restricoes_plano: list | None = None,
                        protocolo: dict | None = None) -> list[dict]:
    """Objetivos iniciais: o alvo da prescrição + os limites do protocolo (QUANTEC/clínica) e as restrições próprias
    dos órgãos presentes, convertidos: Dmax/D…cc → máximo; Dmean → média; V(x Gy) ≤ y% → DVH. Os limites entram
    um pouco abaixo (90%) para a otimização ter folga."""
    import re

    from tps.planejamento import restricoes as R
    prot = protocolo or R.carregar()
    out = []
    if alvo:
        out.append({"estrutura": alvo, "tipo": "alvo", "dose_gy": round(dose_alvo_gy, 2), "volume_pct": None,
                    "peso": 5.0})

    def conv(nome, c):
        m, op, val = c["metrica"], c.get("op", "<="), float(c["valor"])
        if op != "<=" or c.get("tipo") == "objetivo" and False:
            return None
        if m in ("Dmax",) or re.fullmatch(r"D[\d.]+cc", m):
            return {"estrutura": nome, "tipo": "max", "dose_gy": round(0.95 * val, 2), "volume_pct": None, "peso": 1.0}
        if m == "Dmean":
            return {"estrutura": nome, "tipo": "media", "dose_gy": round(0.9 * val, 2), "volume_pct": None, "peso": 1.0}
        mm = re.fullmatch(r"V([\d.]+)Gy", m)
        if mm:
            return {"estrutura": nome, "tipo": "dvh", "dose_gy": float(mm.group(1)), "volume_pct": round(0.9 * val, 1),
                    "peso": 1.0}
        return None
    vistos = set()
    for n in nomes:
        tg = R.nome_tg263(n)
        if not tg or tg == "External" or n == alvo:
            continue
        for c in prot.get("orgaos", {}).get(tg, []):
            if c.get("tipo", "limite") != "limite":
                continue
            o = conv(n, c)
            if o and o["dose_gy"] < dose_alvo_gy * 1.02 and (n, o["tipo"], o["dose_gy"]) not in vistos:
                vistos.add((n, o["tipo"], o["dose_gy"]))
                out.append(o)
    for r in restricoes_plano or []:
        if r.get("tipo") == "limite" and r.get("estrutura") in nomes:
            o = conv(r["estrutura"], r)
            if o:
                out.append(o)
    return out
