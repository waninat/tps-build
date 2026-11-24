"""IMRT step-and-shoot com campos fixos (Elekta Precise/MLCi e Synergy/Agility). SRS-IMR-001..040.

Modelo misto Eclipse + Monaco:

1. **Fluência** (Eclipse): mapa F(par, coluna 2,5 mm) por campo; dose rápida = dose do campo aberto (CCC, por UM) ×
   F borrada (penumbra/espalhamento) ao longo do raio + correção voxel a voxel vinda da dose real ("dose correction
   loop"). **Avoidance** como no Eclipse: raios que cruzam uma estrutura antes do alvo (entrada), depois (saída) ou
   em qualquer ponto (ambos) ficam com fluência zero. **Auto-flash** como no Monaco: alvo a poucos mm da pele →
   a abertura e a fluência se estendem para fora do corpo (margem de flash), para cobrir o movimento da mama.
2. **Sequenciamento** sweep (Bortfeld 1994) respeitando gap e não-interdigitação do MLCi.
3. **Segmentos**: dose de cada segmento pelo CCC (numa grade 2× mais grossa durante as iterações, na grade do plano
   no final) e pesos (UM) otimizados com a dose exata; segmentos fracos saem. **Forma dos segmentos** (como o
   "segment shape optimization" do Monaco): posições das lâminas e pesos otimizados juntos no modelo calibrado,
   com a mecânica como penalidade, e as formas novas recalculadas no CCC.

Custo: objetivos ponderados (Eclipse) ou **restritos** (Monaco "constrained"): os restritos são tratados por
Lagrangiano aumentado — o otimizador primeiro cumpre os limites dos órgãos e depois busca o melhor alvo possível.
Tipos: alvo (uniforme), mínimo, máximo, média, ponto de DVH, **serial** (EUD generalizado com expoente k, órgãos em
série: medula, tronco) e **paralelo** (fração do volume acima da dose ≤ v%, órgãos em paralelo: pulmão, parótida),
além do tecido normal automático (NTO do Eclipse). Vários alvos com doses diferentes (boost integrado) entram como
vários objetivos de alvo.
"""
from __future__ import annotations

import copy
import tempfile
import time
from pathlib import Path
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
TIPOS = ("alvo", "min", "max", "media", "dvh", "serial", "paralelo")
ESCALA = {"alvo": 1.0, "min": 50.0, "max": 20.0, "media": 20.0, "dvh": 20.0, "serial": 20.0, "paralelo": 20.0}


@dataclass
class Objetivo:
    nome: str
    mascara: np.ndarray
    tipo: str                         # alvo | min | max | media | dvh | serial | paralelo
    dose_gy: float                    # dose total (Gy)
    volume_pct: float | None = None   # dvh/paralelo: no máximo volume_pct% acima de dose_gy; serial: expoente k
    peso: float = 1.0
    restrito: bool = False            # Monaco "constrained": tem de ser cumprido


@dataclass
class ResultadoIMRT:
    n_segmentos: dict
    um_total: float
    custo: list = field(default_factory=list)
    metricas: dict = field(default_factory=dict)
    avisos: list = field(default_factory=list)
    tempo_s: float = 0.0
    restricoes: dict = field(default_factory=dict)     # nome/tipo → violação final (0 = cumprida)


def _termo(o: Objetivo, d: np.ndarray):
    """(valor ≥ 0, gradiente em relação às doses) da função de penalidade de um objetivo, sem peso."""
    n = len(d)
    L = o.dose_gy
    if o.tipo == "alvo":
        r = d / L - 1.0
        sub = np.minimum(d / L - 0.95, 0.0)
        sob = np.maximum(d / L - 1.07, 0.0)
        return (r @ r + 50 * sub @ sub + 50 * sob @ sob) / n, 2 * (r + 50 * sub + 50 * sob) / (n * L)
    if o.tipo == "min":
        sub = np.minimum(d / L - 1.0, 0.0)
        return (sub @ sub) / n, 2 * sub / (n * L)
    if o.tipo == "max":
        e = np.maximum(d / L - 1.0, 0.0)
        return (e @ e) / n, 2 * e / (n * L)
    if o.tipo == "media":
        e = max(d.mean() / L - 1.0, 0.0)
        return e * e, np.full(n, 2 * e / (L * n))
    if o.tipo == "dvh":
        k = int(np.floor(n * (o.volume_pct or 0) / 100.0))
        sel = np.argsort(d)[::-1][k:]
        g = np.zeros(n)
        if not len(sel):
            return 0.0, g
        e = np.maximum(d[sel] / L - 1.0, 0.0)
        g[sel] = 2 * e / (len(sel) * L)
        return (e @ e) / len(sel), g
    if o.tipo == "serial":
        k = float(o.volume_pct or 8.0)
        dn = np.maximum(d / L, 1e-6)
        m = float(np.mean(dn ** k))
        eud = m ** (1.0 / k)
        e = max(eud - 1.0, 0.0)
        if e <= 0:
            return 0.0, np.zeros(n)
        deud = eud ** (1 - k) * dn ** (k - 1) / n / L
        return e * e, 2 * e * deud
    if o.tipo == "paralelo":
        w = 0.04 * L
        sg = 1.0 / (1.0 + np.exp(-(d - L) / w))
        e = max(float(sg.mean()) - (o.volume_pct or 0) / 100.0, 0.0)
        return e * e, 2 * e * sg * (1 - sg) / (w * n)
    raise ValueError(f"Tipo de objetivo desconhecido: {o.tipo}")


class Custo:
    """Custo sobre as doses (Gy, dose total) nos voxels amostrados, com gradiente por voxel. Objetivos restritos entram
    pelo Lagrangiano aumentado: λ·g + ρ/2·g², g = √(penalidade) ≈ excesso relativo RMS; `atualizar` sobe os λ."""

    def __init__(self, objetivos: list[Objetivo], idx_por_obj: list[np.ndarray], nto: tuple | None,
                 rho: float = 50.0, tolerancia: float = 0.005):
        self.obj = objetivos
        self.idx = idx_por_obj
        self.nto = nto                # (índices, limite por voxel em Gy, peso)
        self.lam = np.array([10.0 * o.peso if o.restrito else 0.0 for o in objetivos])
        self.rho = rho
        self.tol = tolerancia

    def violacoes(self, D: np.ndarray) -> np.ndarray:
        out = np.zeros(len(self.obj))
        for k, (o, ii) in enumerate(zip(self.obj, self.idx)):
            if o.restrito and len(ii):
                out[k] = np.sqrt(_termo(o, D[ii])[0])
        return out

    def atualizar(self, D: np.ndarray) -> float:
        g = self.violacoes(D)
        self.lam += self.rho * g
        return float(g.max()) if len(g) else 0.0

    def __call__(self, D: np.ndarray):
        f = 0.0
        gD = np.zeros_like(D)
        for k, (o, ii) in enumerate(zip(self.obj, self.idx)):
            if len(ii) == 0:
                continue
            v, gv = _termo(o, D[ii])
            if o.restrito:
                g = np.sqrt(v)
                if g > 1e-9:
                    f += self.lam[k] * g + 0.5 * self.rho * g * g
                    np.add.at(gD, ii, (self.lam[k] + self.rho * g) * gv / (2 * g))
            else:
                esc = ESCALA[o.tipo] * o.peso
                f += esc * v
                np.add.at(gD, ii, esc * gv)
        if self.nto is not None:
            ii, lim, p = self.nto
            e = np.maximum(D[ii] / lim - 1.0, 0.0)
            f += 10 * p * (e @ e) / len(ii)
            np.add.at(gD, ii, 20 * p * e / (len(ii) * lim))
        return f, gD


def _minimizar_al(fun, x0, bounds, custo: Custo, dose_de, rodadas: int = 6, maxiter: int = 300, aviso=None):
    """L-BFGS-B dentro do laço do Lagrangiano aumentado (só faz várias rodadas se houver objetivo restrito)."""
    x = x0
    tem = any(o.restrito for o in custo.obj)
    for r in range(rodadas if tem else 1):
        res = minimize(fun, x, jac=True, method="L-BFGS-B", bounds=bounds,
                       options={"maxiter": maxiter, "ftol": 1e-13, "gtol": 1e-11})
        x = res.x
        if not tem:
            break
        viol = custo.atualizar(dose_de(x))
        if aviso:
            aviso(f"Restrições: maior violação {viol * 100:.1f}% (rodada {r + 1})")
        if viol <= custo.tol:
            break
    return x


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


def _amostrar_raios(grade, c: Campo, u, v, posicao, mascaras: list[np.ndarray]):
    """Amostra máscaras ao longo do raio de cada célula (par, coluna): [n_masc, nv, nu, nt] bool e as distâncias."""
    from scipy.ndimage import map_coordinates
    fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, posicao)
    cantos = np.array([[x, y, z] for x in (grade.x[0], grade.x[-1]) for y in (grade.y[0], grade.y[-1])
                       for z in (grade.z[0], grade.z[-1])])
    dist = np.linalg.norm(cantos - fr.source, axis=1)
    passo = abs(grade.x[1] - grade.x[0])
    ts = np.arange(dist.min(), dist.max() + passo, passo)
    iso_plano = fr.source + 1000.0 * fr.w
    U, V = np.meshgrid(u, v)
    P = iso_plano + U[..., None] * fr.u + V[..., None] * fr.v
    d = P - fr.source
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    out = []
    for m in mascaras:
        res = np.zeros(U.shape + (len(ts),), bool)
        for k0 in range(0, len(ts), 64):                   # em blocos de profundidade (memória)
            tt = ts[k0:k0 + 64]
            a = fr.source + tt[None, None, :, None] * d[:, :, None, :]
            ix = (a[..., 0] - grade.x[0]) / (grade.x[1] - grade.x[0])
            iy = (a[..., 1] - grade.y[0]) / (grade.y[1] - grade.y[0])
            iz = (a[..., 2] - grade.z[0]) / (grade.z[1] - grade.z[0])
            res[:, :, k0:k0 + 64] = map_coordinates(m.astype(np.float32), [iz.ravel(), iy.ravel(), ix.ravel()],
                                                    order=0, mode="constant").reshape(ix.shape) > 0.5
        out.append(res)
    return out


def bloqueio_avoidance(grade, c: Campo, u, v, posicao, alvo: np.ndarray, evitar: list[tuple]) -> np.ndarray:
    """Células (par, coluna) bloqueadas pela avoidance do Eclipse. evitar: [(máscara, "entrada"|"saida"|"ambos")].
    Entrada: o raio passa pela estrutura antes de chegar ao alvo; saída: depois de sair do alvo; ambos: qualquer.
    Raio que não toca o alvo e cruza a estrutura é bloqueado em qualquer modo."""
    bloq = np.zeros((len(v), len(u)), bool)
    if not evitar:
        return bloq
    amostras = _amostrar_raios(grade, c, u, v, posicao, [alvo] + [m for m, _ in evitar])
    A = amostras[0]
    nt = A.shape[-1]
    idx_t = np.arange(nt)
    tem_alvo = A.any(axis=-1)
    t_ini = np.where(tem_alvo, np.argmax(A, axis=-1), nt)
    t_fim = np.where(tem_alvo, nt - 1 - np.argmax(A[..., ::-1], axis=-1), -1)
    for (m, modo), Ev in zip(evitar, amostras[1:]):
        if modo not in ("entrada", "saida", "ambos"):
            raise ValueError(f"Modo de avoidance desconhecido: {modo}")
        qualquer = Ev.any(axis=-1)
        antes = (Ev & (idx_t[None, None, :] < t_ini[..., None])).any(axis=-1)
        depois = (Ev & (idx_t[None, None, :] > t_fim[..., None])).any(axis=-1)
        b = {"entrada": antes, "saida": depois, "ambos": qualquer}[modo]
        bloq |= b | (qualquer & ~tem_alvo)
    return bloq


def aplicar_flash(c: Campo, colim: Colimacao, grade, corpo: np.ndarray, alvo: np.ndarray, flash_mm: float,
                  pele_mm: float, posicao: str) -> bool:
    """Auto-flash (Monaco): onde o alvo está a até `pele_mm` da pele, a abertura do campo se estende `flash_mm` para
    fora do corpo. Muda as lâminas/jaws do campo; devolve True se houve flash."""
    from tps.planejamento.ajuste_mlc import Margem, RasterBEV, ajustar, bev_da_estrutura, dilatar, grade_bev
    sampling = (abs(grade.z[1] - grade.z[0]), abs(grade.y[1] - grade.y[0]), abs(grade.x[1] - grade.x[0]))
    prof = distance_transform_edt(corpo, sampling=sampling)
    sup = alvo & corpo & (prof <= pele_mm)
    if not sup.any():
        return False
    eixos = (grade.x, grade.y, grade.z)
    uu, vv = grade_bev(colim.jaw_y_max_mm + 5, 2.0)
    p_sup = bev_da_estrutura(sup, eixos, c, uu, vv, posicao=posicao)
    p_corpo = bev_da_estrutura(corpo, eixos, c, uu, vv, posicao=posicao)
    flash = dilatar(p_sup, Margem.uniforme(flash_mm)).m & ~p_corpo.m
    if flash.sum() * 4.0 / 100.0 < 0.5:                # < 0,5 cm²
        return False
    from tps.planejamento.ajuste_mlc import raster_da_abertura
    atual = raster_da_abertura(c, colim, uu, vv).m
    r = ajustar(copy.deepcopy(c), colim, [], [], None, OpcoesAjuste(ponto_encontro="fora", resolucao_mm=2.0),
                abertura_base=RasterBEV(uu, vv, atual | flash))
    c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y = r.campo.mlc_a, r.campo.mlc_b, r.campo.jaw_x, r.campo.jaw_y
    return True


def _grade_grossa(grade: calculo.GradeDose, f: int = 2) -> calculo.GradeDose:
    """Grade f× mais grossa (média de η por bloco), para calcular os segmentos durante as iterações."""
    nz, ny, nx = (n // f * f for n in grade.eta.shape)
    e = grade.eta[:nz, :ny, :nx].reshape(nz // f, f, ny // f, f, nx // f, f).mean(axis=(1, 3, 5))
    ax = lambda a, n: a[:n].reshape(-1, f).mean(axis=1)  # noqa: E731
    return calculo.GradeDose(ax(grade.x, nx), ax(grade.y, ny), ax(grade.z, nz), e.astype(np.float32))


def _amostrar_grossa(d: np.ndarray, gg: calculo.GradeDose, pts: tuple) -> np.ndarray:
    from scipy.ndimage import map_coordinates
    xs, ys, zs = pts
    ix = (xs - gg.x[0]) / (gg.x[1] - gg.x[0])
    iy = (ys - gg.y[0]) / (gg.y[1] - gg.y[0])
    iz = (zs - gg.z[0]) / (gg.z[1] - gg.z[0])
    return map_coordinates(d, [iz, iy, ix], order=1, mode="nearest").astype(np.float32)


def _cobertura(a, b, u, res):
    """Fração de cada célula coberta pela abertura [a, b] (linhas × colunas) e as derivadas em a e b."""
    uL, uR = u - res / 2, u + res / 2
    lo = np.maximum(a[:, None], uL[None, :])
    hi = np.minimum(b[:, None], uR[None, :])
    x = (hi - lo) / res
    m = np.clip(x, 0.0, 1.0)
    dentro = (x > 0) & (x < 1)
    da = np.where(dentro & (a[:, None] > uL[None, :]), -1.0 / res, 0.0)
    db = np.where(dentro & (b[:, None] < uR[None, :]), 1.0 / res, 0.0)
    return m, da, db


def otimizar_forma(segs: list, w: np.ndarray, campos, mapas, mod: "ModeloFluencia", custo: Custo, colim: Colimacao,
                   mu0: dict, abertos: dict, maxiter: int = 150) -> tuple[list, np.ndarray, list[bool]]:
    """Segment shape optimization: posições das lâminas (pares abertos) e pesos de todos os segmentos juntos, no
    modelo rápido já calibrado pela dose real. Gap e não-interdigitação entram como penalidade e são conferidos no
    fim; segmento que não passar volta à forma original. Devolve (segmentos, pesos, mudou[])."""
    g = colim.gap_minimo_mm
    interdig = getattr(colim, "interdigitacao", True)
    lim = np.asarray(colim.limites_laminas)
    blocos = {c.nome: bl for c, bl in zip(campos, mod.blocos)}
    params = []                    # por segmento: (campo, linhas abertas, ofs no vetor)
    x0, bnds = [], []
    for (c, s), wi in zip(segs, w):
        u, v, ab = mapas[c.nome]
        a0, b0 = np.asarray(s["mlc_a"], float), np.asarray(s["mlc_b"], float)
        linhas = np.nonzero((b0 - a0 > g + 0.05) & (v > s["jaw_y"][0]) & (v < s["jaw_y"][1]))[0]
        env_a, env_b = np.asarray(abertos[c.nome][0], float), np.asarray(abertos[c.nome][1], float)
        params.append((c, linhas, len(x0)))
        for i in linhas:
            x0 += [a0[i], b0[i]]
            bnds += [(env_a[i], env_b[i] - g), (env_a[i] + g, env_b[i])]
    nlam = len(x0)
    x0 = np.r_[np.asarray(x0, float), np.asarray(w, float)]
    bnds += [(0.0, None)] * len(w)

    def fluencia(x):
        F = np.zeros(mod.n)
        cache = []
        for k, ((c, s), (cc, linhas, o)) in enumerate(zip(segs, params)):
            u, v, ab = mapas[c.nome]
            bl = blocos[c.nome]
            a = x[o:o + 2 * len(linhas):2]
            b = x[o + 1:o + 2 * len(linhas):2]
            m, da, db = _cobertura(a, b, u, u[1] - u[0])
            m *= ab[linhas]
            Fc = F[bl["ofs"]:bl["ofs"] + bl["n"]].reshape(bl["shape"])
            Fc[linhas] += x[nlam + k] / mu0[c.nome] * m
            cache.append((m, da * ab[linhas], db * ab[linhas]))
        return F, cache

    def fun(x):
        F, cache = fluencia(x)
        D = mod.dose(F)
        f, gD = custo(D)
        gF = mod.grad(gD)
        gx = np.zeros_like(x)
        pen = 0.0
        for k, ((c, s), (cc, linhas, o), (m, da, db)) in enumerate(zip(segs, params, cache)):
            bl = blocos[c.nome]
            gFc = gF[bl["ofs"]:bl["ofs"] + bl["n"]].reshape(bl["shape"])[linhas]
            wk = x[nlam + k] / mu0[c.nome]
            gx[o:o + 2 * len(linhas):2] = wk * (gFc * da).sum(axis=1)
            gx[o + 1:o + 2 * len(linhas):2] = wk * (gFc * db).sum(axis=1)
            gx[nlam + k] = (gFc * m).sum() / mu0[c.nome]
            a = x[o:o + 2 * len(linhas):2]
            b = x[o + 1:o + 2 * len(linhas):2]
            e = np.maximum(a - b + g, 0.0)                         # gap mínimo
            pen += float(e @ e)
            gx[o:o + 2 * len(linhas):2] += 2 * e * 1e-2
            gx[o + 1:o + 2 * len(linhas):2] -= 2 * e * 1e-2
            if not interdig and len(linhas) > 1:                   # MLCi: A_i ≤ B_{i±1} − gap entre vizinhos abertos
                viz = np.nonzero(np.diff(linhas) == 1)[0]
                for j0, j1 in ((viz, viz + 1), (viz + 1, viz)):
                    e2 = np.maximum(a[j0] - b[j1] + g, 0.0)
                    pen += float(e2 @ e2)
                    np.add.at(gx, o + 2 * j0, 2 * e2 * 1e-2)
                    np.add.at(gx, o + 2 * j1 + 1, -2 * e2 * 1e-2)
        return f + 1e-2 * pen, gx

    r = minimize(fun, x0, jac=True, method="L-BFGS-B", bounds=bnds,
                 options={"maxiter": maxiter, "ftol": 1e-13, "gtol": 1e-11})
    x = r.x
    novos, mudou = [], []
    for k, ((c, s), (cc, linhas, o)) in enumerate(zip(segs, params)):
        a = np.asarray(s["mlc_a"], float).copy()
        b = np.asarray(s["mlc_b"], float).copy()
        a[linhas] = np.round(x[o:o + 2 * len(linhas):2], 1)
        b[linhas] = np.round(x[o + 1:o + 2 * len(linhas):2], 1)
        cs = Campo(c.nome, c.gantry, c.colimador, c.mesa, c.isocentro_mm, jaw_x=s["jaw_x"], jaw_y=s["jaw_y"],
                   mlc_a=a.tolist(), mlc_b=b.tolist())
        if len(linhas) and not verificar_mecanica(cs, colim) and \
                np.abs(np.r_[a - np.asarray(s["mlc_a"]), b - np.asarray(s["mlc_b"])]).max() >= 0.5:
            jx = s["jaw_x"]
            if jx is not None:
                jx = [max(abertos[c.nome][2][0], float(a[linhas].min()) - 2.0),
                      min(abertos[c.nome][2][1], float(b[linhas].max()) + 2.0)] if abertos[c.nome][2] else jx
            novos.append({**s, "mlc_a": a.tolist(), "mlc_b": b.tolist(), "jaw_x": jx})
            mudou.append(True)
        else:
            novos.append(s)
            mudou.append(False)
    return novos, np.maximum(x[nlam:], 0.0), mudou


def otimizar_imrt(plano: Plano, grade: calculo.GradeDose, doses_um: dict, objetivos: list[Objetivo],
                  corpo: np.ndarray, colim: Colimacao, modelo, niveis: int = 7, iteracoes: int = 2,
                  res_u_mm: float = 2.5, sigma_mm: tuple = (3.0, 3.5), um_min_segmento: float = 3.0,
                  area_min_cm2: float = 4.0, peso_nto: float = 1.0, suavidade: float = 0.05, kernels=None,
                  posicao: str = "HFS", permitir_demonstracao: bool = False, progresso=None,
                  evitar: list | None = None, flash_mm: float = 0.0, flash_pele_mm: float = 5.0,
                  forma_segmentos: bool = True, grade_grossa: bool = True) -> ResultadoIMRT:
    """Otimiza e segmenta todos os campos principais do plano (que já devem estar conformados aos alvos).
    evitar: [(nome, máscara, modo)] avoidance; flash_mm > 0 liga o auto-flash."""
    t0 = time.time()
    plano._exigir_rascunho()
    aviso = progresso or (lambda m: None)
    campos = [c for c in plano.campos if c.campo_pai is None]
    if not campos:
        raise ValueError("Crie os campos (ângulos) antes de otimizar")
    alvos = [o for o in objetivos if o.tipo == "alvo"]
    if not alvos:
        raise ValueError("Defina pelo menos um alvo com dose")
    for o in objetivos:
        if o.tipo not in TIPOS:
            raise ValueError(f"Tipo de objetivo desconhecido: {o.tipo}")
    fx = plano.prescricao.fracoes
    rng = np.random.default_rng(0)
    avisos = []
    todos_alvos = np.zeros_like(corpo)
    for o in alvos:
        todos_alvos |= o.mascara
    for c in campos:                                     # parte dos campos abertos conformados
        c.segmentos = None
    if flash_mm > 0:
        com = [c.nome for c in campos if aplicar_flash(c, colim, grade, corpo, todos_alvos, flash_mm, flash_pele_mm,
                                                       posicao)]
        for n in com:
            doses_um.pop(n, None)
        if com:
            avisos.append(f"Auto-flash de {flash_mm:g} mm em {', '.join(com)}")
    faltam = [c for c in campos if c.nome not in doses_um]
    if faltam:
        aviso("Calculando os campos abertos…")
        doses_um.update(calculo.dose_por_um(grade, faltam, colim, modelo, kernels, posicao, permitir_demonstracao))
    # tecido normal: corpo fora dos alvos (+3 mm), limite caindo com a distância (NTO)
    passo = float(abs(grade.x[1] - grade.x[0]))
    dist = distance_transform_edt(~todos_alvos, sampling=(abs(grade.z[1] - grade.z[0]), abs(grade.y[1] - grade.y[0]),
                                                           passo))
    rx_max = max(o.dose_gy for o in alvos)
    normal = corpo & (dist > 3.0)
    mascs = [o.mascara for o in objetivos] + [normal]
    nmax = [15000 if o.tipo == "alvo" else 6000 for o in objetivos] + [20000]
    idx, partes = _amostras(mascs, nmax, rng)
    lim_nto = rx_max * (0.55 + 0.50 * np.exp(-0.05 * (dist.ravel()[idx[partes[-1]]] - 3.0)))
    custo = Custo(objetivos, partes[:-1], (partes[-1], lim_nto, peso_nto) if peso_nto > 0 else None)
    kz, ky, kx = np.unravel_index(idx, grade.eta.shape)
    pts = (grade.x[kx], grade.y[ky], grade.z[kz])
    gg = _grade_grossa(grade) if grade_grossa and passo < 4.5 else None
    mapas = {c.nome: _grade_fluencia(c, colim, res_u_mm) for c in campos}
    # avoidance: células bloqueadas ficam fora da abertura otimizável
    if evitar:
        for c in campos:
            u, v, ab = mapas[c.nome]
            bq = bloqueio_avoidance(grade, c, u, v, posicao, todos_alvos, [(m, modo) for _, m, modo in evitar])
            mapas[c.nome] = (u, v, ab & ~bq)
            if bq.any():
                avisos.append(f"{c.nome}: avoidance bloqueou {int((ab & bq).sum())} de {int(ab.sum())} células")
    # flash: células da abertura sem corpo no raio recebem a fluência vizinha de dentro (não são otimizadas)
    zona_flash = {}
    if flash_mm > 0:
        from tps.planejamento.ajuste_mlc import bev_da_estrutura
        for c in campos:
            u, v, ab = mapas[c.nome]
            pc = bev_da_estrutura(corpo, (grade.x, grade.y, grade.z), c, u, v, posicao=posicao).m
            zona_flash[c.nome] = ab & ~pc
    mu0 = {c.nome: float(c.mu or 100.0) for c in campos}
    d_ab = {c.nome: fx * mu0[c.nome] * doses_um[c.nome].ravel()[idx] for c in campos}
    lim = np.asarray(colim.limites_laminas)
    sig = (sigma_mm[0] / float(lim[1] - lim[0]), sigma_mm[1] / res_u_mm)
    mod = ModeloFluencia(campos, mapas, grade, idx, d_ab, sig, posicao)
    livres = mod.livres()
    for bl, c in zip(mod.blocos, campos):
        if c.nome in zona_flash:
            livres[bl["ofs"]:bl["ofs"] + bl["n"]] &= ~zona_flash[c.nome].ravel()
    F = np.where(livres, 1.0, 0.0)
    D0 = mod.dose(F)
    a0 = alvos[0]
    F *= a0.dose_gy / max(float(D0[partes[objetivos.index(a0)]].mean()), 1e-9)
    hist = []
    melhor = None
    abertos_orig = {c.nome: (list(c.mlc_a), list(c.mlc_b), c.jaw_x, c.jaw_y) for c in campos}
    ver = {}                                             # chave do segmento → dose/UM (Gy) nas amostras
    pasta_tmp = tempfile.TemporaryDirectory(prefix="tps_imrt_")

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

    def calcular_segmentos(lista_cs, fina: bool):
        """Dose/UM de cada segmento nas amostras (grade grossa nas iterações; fina no final, volume em disco)."""
        out = {}
        for j, (cs, ch) in enumerate(lista_cs):
            if j % 10 == 0:
                aviso(f"Calculando os segmentos com o motor completo… {j}/{len(lista_cs)}"
                      + ("" if fina or gg is None else " (grade grossa)"))
            if fina or gg is None:
                d = calculo.dose_por_um(grade, [cs], colim, modelo, kernels, posicao, permitir_demonstracao)[cs.nome]
                arq = Path(pasta_tmp.name) / f"s{len(os_arquivos):05d}.npy"
                np.save(arq, d.astype(np.float32))
                os_arquivos[ch] = arq
                out[ch] = d.ravel()[idx].astype(np.float32)
            else:
                d = calculo.dose_por_um(gg, [cs], colim, modelo, kernels, posicao, permitir_demonstracao)[cs.nome]
                out[ch] = _amostrar_grossa(d, gg, pts)
            del d
        return out
    os_arquivos = {}

    def chave(c, s):
        return (c.nome, tuple(s["mlc_a"]), tuple(s["mlc_b"]), tuple(s["jaw_y"]))

    def campo_seg(c, s, j):
        return Campo(f"{c.nome}#s{j}", c.gantry, c.colimador, c.mesa, c.isocentro_mm, jaw_x=s["jaw_x"],
                     jaw_y=s["jaw_y"], mlc_a=s["mlc_a"], mlc_b=s["mlc_b"])

    def pesos(A, w0, ativo, areas, rodadas=4):
        for _ in range(3):
            def obj_w(w):
                f, gD = custo(A @ w)
                return f, A.T @ gD
            bnds = [(0.0, None) if a_ else (0.0, 0.0) for a_ in ativo]
            w = _minimizar_al(obj_w, np.where(ativo, w0, 0.0), bnds, custo, lambda x: A @ x, rodadas, 500)
            fracos = ativo & ((w < um_min_segmento) | (areas < area_min_cm2))
            if not fracos.any():
                break
            ativo = ativo & ~fracos
            w0 = w
        return np.where(ativo, w, 0.0), ativo

    def fluencia_entregue(lista, w):
        Fr = np.zeros(mod.n)
        for bl, c in zip(mod.blocos, campos):
            u, v, ab = mapas[c.nome]
            fq = np.zeros(bl["shape"])
            for (cc, s), wi in zip(lista, w):
                if cc is not c or wi <= 0:
                    continue
                dentro = (v > s["jaw_y"][0]) & (v < s["jaw_y"][1])
                m, _, _ = _cobertura(np.asarray(s["mlc_a"], float), np.asarray(s["mlc_b"], float), u, u[1] - u[0])
                fq += wi / mu0[c.nome] * m * dentro[:, None]
            Fr[bl["ofs"]:bl["ofs"] + bl["n"]] = fq.ravel()
        return Fr

    for it in range(iteracoes + 1):
        # ---- 1. fluência
        aviso(f"Otimizando a fluência (iteração {it + 1})…")

        def obj(Fv):
            f, gD = custo(mod.dose(Fv))
            fs, gs = suav(Fv)
            return f + fs, (mod.grad(gD) + gs) * livres

        F = _minimizar_al(obj, F, [(0.0, None) if lv else (0.0, 0.0) for lv in livres], custo, mod.dose, 4, 400, aviso)
        # ---- 2. sequenciamento (flash: fluência da célula de dentro mais próxima)
        aviso("Sequenciando os segmentos…")
        lista, unid = [], []
        for bl, c in zip(mod.blocos, campos):
            u, v, ab = mapas[c.nome]
            Fc = np.where(ab, F[bl["ofs"]:bl["ofs"] + bl["n"]].reshape(bl["shape"]), 0.0)
            if c.nome in zona_flash and zona_flash[c.nome].any():
                zf = zona_flash[c.nome]
                dentro = ab & ~zf
                if dentro.any():
                    _, (ii, jj) = distance_transform_edt(~dentro, return_indices=True)
                    Fc = np.where(zf, Fc[ii, jj], Fc)
            fmax = float(Fc.max())
            if fmax <= 1e-6:
                continue
            delta = fmax / niveis
            c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y = copy.deepcopy(abertos_orig[c.nome])
            for s in sequenciar_sweep(c, colim, u, np.rint(Fc / delta).astype(int), ab):
                lista.append((c, s))
                unid.append(s["peso"] * delta * mu0[c.nome])
        # ---- 3. dose dos segmentos e pesos
        novos = [(campo_seg(c, s, j), chave(c, s)) for j, (c, s) in enumerate(lista) if chave(c, s) not in ver]
        aviso(f"Calculando {len(novos)} segmento(s) com o motor completo…")
        ver.update(calcular_segmentos(novos, fina=False))
        A = np.column_stack([fx * ver[chave(c, s)] for c, s in lista])
        areas = np.array([_area_cm2(s, colim) for _, s in lista])
        w, ativo = pesos(A, np.asarray(unid), np.ones(len(lista), bool), areas)
        lista = [q for q, a in zip(lista, ativo) if a]
        A, w = A[:, ativo], w[ativo]
        # ---- 3b. forma dos segmentos (última iteração)
        if forma_segmentos and it == iteracoes and lista:
            mod.corr = mod.corr + (A @ w) - mod.dose(fluencia_entregue(lista, w))
            aviso("Otimizando a forma dos segmentos…")
            antes = float(custo(A @ w)[0])
            segs2, w2, mudou = otimizar_forma(lista, w, campos, mapas, mod, custo, colim, mu0, abertos_orig)
            lista2 = [(c, s2) for (c, _), s2 in zip(lista, segs2)]
            novos = [(campo_seg(c, s, j), chave(c, s)) for j, ((c, s), m) in enumerate(zip(lista2, mudou))
                     if m and chave(c, s) not in ver]
            aviso(f"Recalculando {len(novos)} segmento(s) com a forma nova…")
            ver.update(calcular_segmentos(novos, fina=False))
            A2 = np.column_stack([fx * ver[chave(c, s)] for c, s in lista2])
            areas2 = np.array([_area_cm2(s, colim) for _, s in lista2])
            w2, at2 = pesos(A2, w2, np.ones(len(lista2), bool), areas2)
            depois = float(custo(A2 @ w2)[0])
            if depois < antes:
                lista, A, w = [q for q, a in zip(lista2, at2) if a], A2[:, at2], w2[at2]
                avisos.append(f"Forma dos segmentos: custo {antes:.4f} → {depois:.4f} ({sum(mudou)} segmentos mudaram)")
        fval = float(custo(A @ w)[0])
        hist.append(fval)
        aviso(f"Custo {fval:.4f} ({len(lista)} segmentos)")
        if melhor is None or fval < melhor[0]:
            melhor = (fval, list(lista), w.copy())
        if it == iteracoes:
            break
        # correção do modelo rápido: dose real − modelo com a fluência entregue
        mod.corr[:] = 0.0
        mod.corr = (A @ w) - mod.dose(fluencia_entregue(lista, w))
    # ---- final: segmentos escolhidos na grade do plano, pesos refeitos com a dose fina
    _, lista, w = melhor
    aviso(f"Calculando os {len(lista)} segmentos finais na grade do plano…")
    finos = calcular_segmentos([(campo_seg(c, s, j), chave(c, s)) for j, (c, s) in enumerate(lista)], fina=True)
    A = np.column_stack([fx * finos[chave(c, s)] for c, s in lista])
    areas = np.array([_area_cm2(s, colim) for _, s in lista])
    w, ativo = pesos(A, w, np.ones(len(lista), bool), areas)
    viol = custo.violacoes(A @ w)
    restr = {f"{o.nome} {o.tipo} {o.dose_gy:g}": round(float(v) * 100, 2) for o, v in zip(objetivos, viol)
             if o.restrito}
    for c in campos:
        meus = [(s, wi) for (cc, s), wi in zip(lista, w) if cc is c and wi > 0]
        c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y = copy.deepcopy(abertos_orig[c.nome])
        if not meus:
            avisos.append(f"{c.nome}: nenhum segmento útil (campo sem peso)")
            c.segmentos = None
            continue
        tot = sum(wi for _, wi in meus)
        c.segmentos = [{"mlc_a": s["mlc_a"], "mlc_b": s["mlc_b"], "jaw_x": s["jaw_x"], "jaw_y": s["jaw_y"],
                        "peso": wi / tot} for s, wi in meus]
        c.peso = tot
        c.mlc_a = [min(s["mlc_a"][i] for s, _ in meus) for i in range(colim.n_pares)]
        c.mlc_b = [max(s["mlc_b"][i] for s, _ in meus) for i in range(colim.n_pares)]
        doses_um[c.nome] = sum((wi / tot) * np.load(os_arquivos[chave(c, s)]) for s, wi in meus)
    for c in [c for c in campos if c.segmentos is None]:
        plano.campos.remove(c)
        doses_um.pop(c.nome, None)
    pasta_tmp.cleanup()
    for n, v in restr.items():
        if v > 1.0:
            avisos.append(f"Restrição não cumprida: {n} (excesso {v:.1f}%) — o alvo já está no limite; afrouxe ou "
                          "mude os ângulos")
    calculo.normalizar(plano, doses_um, grade, alvos[0].mascara)
    return ResultadoIMRT({c.nome: len(c.segmentos) for c in plano.campos if c.segmentos},
                         round(sum(float(c.mu or 0) for c in plano.campos), 1), hist, {}, avisos,
                         round(time.time() - t0, 1), restr)


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
