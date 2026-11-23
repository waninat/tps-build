"""Ajuste de MLC e jaws na visão do feixe (BEV), com as opções do Eclipse e algumas a mais. SRS-PLN-030..042.

Pipeline de um campo:
  1. `bev_da_estrutura`: projeção cônica (traçado de raios fonte → plano do isocentro) da estrutura 3D; um pixel do
     BEV está no alvo se algum ponto do raio está dentro da estrutura (interpolação linear da máscara ≥ 0,5).
  2. `dilatar`: margem BEV por lado (X1, X2, Y1, Y2), com forma elíptica entre os eixos — como "Margin" do Eclipse.
  3. Abertura = ∪ alvos com margem − ∪ blindagens com margem ("Fit and shield"; o alvo tem prioridade se pedido).
  4. `ajustar_laminas`: posição de cada lâmina pelo critério do ponto de encontro com o contorno:
        "fora"   (Eclipse "Out of field")  lâmina nunca entra na abertura: cobertura total do alvo;
        "dentro" (Eclipse "In field")      lâmina nunca deixa tecido fora da abertura exposto;
        "meio"   (Eclipse "Crossing")      a ponta encontra o contorno no eixo central da lâmina;
        "area"   (novo)                    a ponta fica onde a fração da largura da lâmina coberta pela abertura é 50%:
                                           minimiza a área exposta fora da abertura + área do alvo bloqueada;
        fração t (novo)                    generaliza: 0 = fora, 1 = dentro, 0,5 = área.
  5. Jaws ajustados à abertura (Y) e às lâminas abertas (X, quando existe colimador X — MLCi/MLCi2).
  6. Pares fechados escondidos sob o jaw quando possível; restrição de não-interdigitação do MLCi resolvida por
     propagação (os pares fechados se movem para onde a mecânica permite; se preciso, abre-se o mínimo).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import binary_dilation, map_coordinates

from tps.engine.geometry import beam_frame
from tps.planejamento.conformacao import Colimacao, pontos_do_alvo, projetar_bev
from tps.planejamento.modelo import Campo

PONTOS_DE_ENCONTRO = {"fora": 0.0, "area": 0.5, "dentro": 1.0}


@dataclass
class Margem:
    """Margem BEV (mm) por lado: x1 = lado −u (banco A), x2 = +u (banco B), y1 = −v, y2 = +v."""
    x1: float = 0.0
    x2: float = 0.0
    y1: float = 0.0
    y2: float = 0.0
    eliptica: bool = True

    @classmethod
    def uniforme(cls, m: float) -> "Margem":
        return cls(m, m, m, m)

    def maxima(self) -> float:
        return max(self.x1, self.x2, self.y1, self.y2)


@dataclass
class RasterBEV:
    u: np.ndarray            # centros das colunas (mm no isocentro)
    v: np.ndarray            # centros das linhas
    m: np.ndarray            # [nv, nu] bool

    @property
    def res(self) -> float:
        return float(self.u[1] - self.u[0])

    def vazio_como(self) -> "RasterBEV":
        return RasterBEV(self.u, self.v, np.zeros_like(self.m))

    def area_cm2(self) -> float:
        return float(self.m.sum()) * self.res ** 2 / 100.0


def grade_bev(extensao_mm: float, res_mm: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    n = int(np.ceil(2 * extensao_mm / res_mm))
    ax = (np.arange(n) - (n - 1) / 2) * res_mm
    return ax, ax.copy()


def bev_da_estrutura(mascara: np.ndarray, eixos: tuple, campo: Campo, u: np.ndarray, v: np.ndarray,
                     sad_mm: float = 1000.0, posicao: str = "HFS") -> RasterBEV:
    """Projeção cônica da estrutura no plano do isocentro (traçado de raios)."""
    x, y, z = eixos
    out = np.zeros((len(v), len(u)), bool)
    if not mascara.any():
        return RasterBEV(u, v, out)
    fr = beam_frame(campo.gantry, campo.colimador, campo.mesa, campo.isocentro_mm, sad_mm, posicao)
    hx, hy, hz = (abs(a[1] - a[0]) if len(a) > 1 else 1.0 for a in (x, y, z))
    kz, ky, kx = np.nonzero(mascara)
    # caixa da estrutura → faixa de distâncias ao longo do raio e faixa (u, v) a examinar
    cantos = np.array([[x[i], y[j], z[k]] for i in (kx.min(), kx.max()) for j in (ky.min(), ky.max())
                       for k in (kz.min(), kz.max())])
    cantos = np.vstack([cantos + np.array([sx * hx, sy * hy, sz * hz])
                        for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)])
    dist = np.linalg.norm(cantos - fr.source, axis=1)
    passo = 0.5 * min(hx, hy, hz)
    ts = np.arange(dist.min() - passo, dist.max() + passo, passo)
    pu, pv = projetar_bev(cantos, campo, sad_mm, posicao)
    iu = np.nonzero((u >= pu.min() - 1) & (u <= pu.max() + 1))[0]
    iv = np.nonzero((v >= pv.min() - 1) & (v <= pv.max() + 1))[0]
    if len(iu) == 0 or len(iv) == 0:
        return RasterBEV(u, v, out)
    mf = mascara.astype(np.float32)
    origem = np.array([x[0], y[0], z[0]])
    passo_idx = np.array([x[1] - x[0] if len(x) > 1 else 1.0, y[1] - y[0] if len(y) > 1 else 1.0,
                          z[1] - z[0] if len(z) > 1 else 1.0])
    iso_plano = fr.source + sad_mm * fr.w
    U = u[iu]
    for a, jv in enumerate(iv):                       # uma linha do BEV por vez (memória limitada)
        P = iso_plano[None, :] + U[:, None] * fr.u[None, :] + v[jv] * fr.v[None, :]
        d = P - fr.source
        d /= np.linalg.norm(d, axis=1)[:, None]
        pts = fr.source[None, None, :] + ts[None, :, None] * d[:, None, :]        # [nu, nt, 3]
        idx = (pts - origem) / passo_idx
        val = map_coordinates(mf, [idx[..., 2].ravel(), idx[..., 1].ravel(), idx[..., 0].ravel()],
                              order=1, mode="constant", cval=0.0).reshape(len(U), len(ts))
        out[jv, iu] = val.max(axis=1) >= 0.5
    return RasterBEV(u, v, out)


def dilatar(r: RasterBEV, mg: Margem) -> RasterBEV:
    """Margem BEV assimétrica: cada quadrante do elemento estruturante é uma elipse com os semi-eixos do lado."""
    if mg.maxima() <= 0:
        return RasterBEV(r.u, r.v, r.m.copy())
    if min(mg.x1, mg.x2, mg.y1, mg.y2) < 0:
        raise ValueError("Margem negativa: use blindagem ou contração explícita")
    h = r.res
    nx1, nx2, ny1, ny2 = (int(np.ceil(m / h)) for m in (mg.x1, mg.x2, mg.y1, mg.y2))
    du = np.arange(-nx1, nx2 + 1) * h
    dv = np.arange(-ny1, ny2 + 1) * h
    DU, DV = np.meshgrid(du, dv)
    ax = np.where(DU > 0, mg.x2, mg.x1)
    ay = np.where(DV > 0, mg.y2, mg.y1)
    with np.errstate(divide="ignore", invalid="ignore"):
        tu = np.where(ax > 0, (DU / np.where(ax > 0, ax, 1)) ** 2, np.where(DU == 0, 0.0, np.inf))
        tv = np.where(ay > 0, (DV / np.where(ay > 0, ay, 1)) ** 2, np.where(DV == 0, 0.0, np.inf))
    el = (tu + tv <= 1.0 + 1e-9) if mg.eliptica else ((tu <= 1 + 1e-9) & (tv <= 1 + 1e-9))
    # dilatação: alvo deslocado por cada vetor do elemento (o elemento é usado "espelhado" pela convenção do scipy)
    # Elemento simétrico (quadrado de lado 2·max) com o elemento assimétrico centrado: evita depender da convenção
    # de origem/reflexão do scipy (verificado em tests/test_ajuste_mlc.py::test_margem_assimetrica)
    ny, nx = max(ny1, ny2), max(nx1, nx2)
    est = np.zeros((2 * ny + 1, 2 * nx + 1), bool)
    est[ny - ny1:ny + ny2 + 1, nx - nx1:nx + nx2 + 1] = el
    # (o scipy aplica o elemento sem reflexão neste uso: deslocamento +u do elemento → crescimento para +u)
    m = np.pad(r.m, ((ny, ny), (nx, nx)))
    d = binary_dilation(m, structure=est)
    return RasterBEV(r.u, r.v, d[ny:ny + r.m.shape[0], nx:nx + r.m.shape[1]])


def raster_da_abertura(c: Campo, colim: Colimacao, u: np.ndarray, v: np.ndarray) -> RasterBEV:
    """Área aberta de um campo (MLC ∩ jaws) no raster BEV."""
    U, V = np.meshgrid(u, v)
    lim = colim.limites_laminas
    jy0, jy1 = c.jaw_y
    jx0, jx1 = c.jaw_x if c.jaw_x else (-np.inf, np.inf)
    par = np.clip(np.searchsorted(lim, V, side="right") - 1, 0, colim.n_pares - 1)
    a = np.asarray(c.mlc_a)[par]
    b = np.asarray(c.mlc_b)[par]
    m = (V >= lim[0]) & (V < lim[-1]) & (V >= jy0) & (V <= jy1) & (U >= np.maximum(a, jx0)) & (U <= np.minimum(b, jx1))
    return RasterBEV(u, v, m)


@dataclass
class OpcoesAjuste:
    ponto_encontro: str | float = "fora"     # fora | meio | area | dentro | fração 0..1
    folga_jaw_mm: float = 0.0                 # jaw além da abertura (Eclipse: "jaw margin")
    jaw_x_segue_mlc: bool = True              # colimador X (MLCi) no envelope das lâminas abertas
    fechados: str = "sob_jaw"                 # sob_jaw | centro
    segmentos: str = "envelope"               # envelope (do 1º ao último trecho) | maior (só o maior trecho; FiF)
    resolucao_mm: float = 0.5


@dataclass
class ResultadoAjuste:
    campo: Campo
    abertura: RasterBEV
    avisos: list[str] = field(default_factory=list)
    correcoes_interdigitacao: int = 0


def _limiar(p) -> float:
    if isinstance(p, (int, float)):
        if not 0.0 <= float(p) <= 1.0:
            raise ValueError("Fração do ponto de encontro fora de 0..1")
        return float(p)
    if p == "meio":
        return -1.0                                   # tratado à parte
    if p not in PONTOS_DE_ENCONTRO:
        raise ValueError(f"Ponto de encontro desconhecido: {p}")
    return PONTOS_DE_ENCONTRO[p]


def ajustar_laminas(ab: RasterBEV, colim: Colimacao, op: OpcoesAjuste) -> tuple[list, list, tuple, tuple | None, list]:
    """Lâminas e jaws para uma abertura BEV. Devolve (A, B, jaw_y, jaw_x, avisos)."""
    if not ab.m.any():
        raise ValueError("Abertura vazia: nada a conformar")
    t = _limiar(op.ponto_encontro)
    lim = colim.limites_laminas
    h = ab.res
    avisos = []
    a = np.full(colim.n_pares, np.nan)
    b = np.full(colim.n_pares, np.nan)
    linhas = np.nonzero(ab.m.any(axis=1))[0]
    vmin, vmax = ab.v[linhas[0]] - h / 2, ab.v[linhas[-1]] + h / 2
    jy = (vmin - op.folga_jaw_mm, vmax + op.folga_jaw_mm)
    for i in range(colim.n_pares):
        lo, hi = max(lim[i], jy[0]), min(lim[i + 1], jy[1])
        if hi <= lo:
            continue
        sel = (ab.v >= lo) & (ab.v < hi)
        if not sel.any():
            continue
        faixa = ab.m[sel]                                     # [linhas da faixa, nu]
        if t < 0:                                             # "meio": linha no eixo central visível da lâmina
            centro = 0.5 * (lim[i] + lim[i + 1])
            if not (lo <= centro < hi):
                centro = 0.5 * (lo + hi)
            k = np.argmin(np.abs(ab.v[sel] - centro))
            cob = faixa[k].astype(float)
            ok = cob > 0.5
        else:
            cob = faixa.mean(axis=0)
            ok = cob > 0 if t == 0 else cob >= t - 1e-9
        if not ok.any():
            continue
        cols = np.nonzero(ok)[0]
        quebras = np.nonzero(np.diff(cols) > 1)[0]
        if len(quebras) and op.segmentos == "maior":
            trechos = np.split(cols, quebras + 1)
            cols = max(trechos, key=lambda tr: float(cob[tr].sum()))
        elif len(quebras):                                    # o MLC não blinda o meio de um par
            avisos.append(f"Par {i + 1}: abertura com interrupção — o MLC não consegue blindar o meio do par")
        a[i], b[i] = ab.u[cols[0]] - h / 2, ab.u[cols[-1]] + h / 2
    abertos = ~np.isnan(a)
    if not abertos.any():
        raise ValueError("Nenhum par de lâminas abre com esse ponto de encontro (alvo menor que uma lâmina?)")
    jx = None
    if colim.tem_jaw_x:
        jx = (float(np.nanmin(a)) - op.folga_jaw_mm, float(np.nanmax(b)) + op.folga_jaw_mm) if op.jaw_x_segue_mlc \
            else (-colim.jaw_x_max_mm, colim.jaw_x_max_mm)
    return list(a), list(b), jy, jx, avisos


def _posicionar_fechados(a: np.ndarray, b: np.ndarray, colim: Colimacao, jx, op: OpcoesAjuste) -> tuple[int, list]:
    """Fecha os pares sem abertura e impõe gap mínimo e (se o MLC não interdigita) a restrição entre vizinhos."""
    g = colim.gap_minimo_mm
    n = colim.n_pares
    abertos = ~np.isnan(a)
    avisos = []
    centro = 0.5 * (np.nanmin(a) + np.nanmax(b))
    if op.fechados == "sob_jaw" and jx is not None:
        pref = jx[0] - g / 2 - 1.0                           # gap escondido sob o jaw X1
    else:
        pref = centro
    # gap mínimo nos abertos
    for i in np.nonzero(abertos)[0]:
        if b[i] - a[i] < g:
            c = 0.5 * (a[i] + b[i])
            a[i], b[i] = c - g / 2, c + g / 2
    interdigita = getattr(colim, "interdigitacao", True)
    if interdigita:
        for i in np.nonzero(~abertos)[0]:
            a[i], b[i] = pref - g / 2, pref + g / 2
        return 0, avisos
    # sem interdigitação: propaga a partir dos pares abertos (BFS por distância)
    ordem = sorted(np.nonzero(~abertos)[0], key=lambda i: np.min(np.abs(np.nonzero(abertos)[0] - i)))
    definido = abertos.copy()
    for i in ordem:
        viz = [j for j in (i - 1, i + 1) if 0 <= j < n and definido[j]]
        lo = max((a[j] for j in viz), default=-np.inf) + g / 2
        hi = min((b[j] for j in viz), default=np.inf) - g / 2
        if lo <= hi:
            c = min(max(pref, lo), hi)
            a[i], b[i] = c - g / 2, c + g / 2
        else:                                                # vizinhos incompatíveis: abre o mínimo
            a[i] = min(b[j] for j in viz) - g
            b[i] = max(max(a[j] for j in viz) + g, a[i] + g)
        definido[i] = True
    corr = 0
    for _ in range(4 * n):                                   # vizinhos abertos incompatíveis: alarga
        mudou = False
        for i in range(n - 1):
            j = i + 1
            if a[i] > b[j] - g + 1e-9:
                b[j] = a[i] + g
                mudou = True
                corr += 1
            if a[j] > b[i] - g + 1e-9:
                b[i] = a[j] + g
                mudou = True
                corr += 1
        if not mudou:
            break
    if corr:
        avisos.append(f"Não-interdigitação: {corr} lâmina(s) recuada(s) além do contorno para respeitar a mecânica")
    return corr, avisos


def verificar_mecanica(c: Campo, colim: Colimacao) -> list[str]:
    """Limites mecânicos: gap mínimo, overtravel, extensão, jaws e (MLCi) não-interdigitação."""
    a, b = np.asarray(c.mlc_a, float), np.asarray(c.mlc_b, float)
    g = colim.gap_minimo_mm
    erros = []
    if len(a) != colim.n_pares or len(b) != colim.n_pares:
        return [f"{c.nome}: número de lâminas diferente do MLC ({colim.n_pares} pares)"]
    if np.any(b - a < g - 1e-6):
        erros.append(f"{c.nome}: par com gap menor que {g} mm")
    if np.any(a > colim.overtravel_mm + 1e-6) or np.any(b < -colim.overtravel_mm - 1e-6):
        erros.append(f"{c.nome}: lâmina além do overtravel ({colim.overtravel_mm} mm)")
    if np.any(np.abs(np.r_[a, b]) > colim.lamina_max_mm + 1e-6):
        erros.append(f"{c.nome}: lâmina além do campo máximo")
    ext = getattr(colim, "extensao_banco_mm", None)
    if ext and (a.max() - a.min() > ext + 1e-6 or b.max() - b.min() > ext + 1e-6):
        erros.append(f"{c.nome}: extensão do banco acima de {ext} mm")
    if not getattr(colim, "interdigitacao", True):
        for i in range(colim.n_pares - 1):
            if a[i] > b[i + 1] - g + 1e-6 or a[i + 1] > b[i] - g + 1e-6:
                erros.append(f"{c.nome}: interdigitação entre os pares {i + 1} e {i + 2}")
                break
    jy = c.jaw_y
    if jy is None or jy[0] >= jy[1] or max(abs(jy[0]), abs(jy[1])) > colim.jaw_y_max_mm + 1e-6:
        erros.append(f"{c.nome}: jaw Y inválido")
    if colim.tem_jaw_x and (c.jaw_x is None or c.jaw_x[0] >= c.jaw_x[1]):
        erros.append(f"{c.nome}: jaw X inválido")
    return erros


def ajustar(campo: Campo, colim: Colimacao, alvos: list[tuple], blindagens: list[tuple] | None = None,
            eixos: tuple | None = None, op: OpcoesAjuste | None = None, sad_mm: float = 1000.0,
            posicao: str = "HFS", prioridade_alvo: bool = False, abertura_base: RasterBEV | None = None,
            nome_alvo: str | None = None) -> ResultadoAjuste:
    """Ajusta o campo.

    alvos / blindagens: listas de (máscara 3D, Margem). `abertura_base` (opcional) restringe a abertura (sub-campos
    de field-in-field: abertura do campo pai). prioridade_alvo=True: a blindagem não corta o alvo (sem margem).
    """
    op = op or OpcoesAjuste()
    blindagens = blindagens or []
    if abertura_base is not None:
        u, v = abertura_base.u, abertura_base.v
    else:
        ext = colim.jaw_y_max_mm + 5
        u, v = grade_bev(ext, op.resolucao_mm)
    ab = np.zeros((len(v), len(u)), bool) if (alvos or abertura_base is None) else abertura_base.m.copy()
    alvo_puro = np.zeros_like(ab)
    for masc, mg in alvos:
        r = bev_da_estrutura(masc, eixos, campo, u, v, sad_mm, posicao)
        alvo_puro |= r.m
        ab |= dilatar(r, mg).m
    for masc, mg in blindagens:
        r = dilatar(bev_da_estrutura(masc, eixos, campo, u, v, sad_mm, posicao), mg)
        ab &= ~(r.m & ~alvo_puro) if prioridade_alvo else ~r.m
    if abertura_base is not None:
        ab &= abertura_base.m
    abertura = RasterBEV(u, v, ab)
    a, b, jy, jx, avisos = ajustar_laminas(abertura, colim, op)
    a, b = np.array(a), np.array(b)
    corr, av2 = _posicionar_fechados(a, b, colim, jx, op)
    # arredondamento a 0,1 mm: para fora (cobre o alvo), exceto no ponto de encontro "dentro" (para dentro)
    para_dentro = op.ponto_encontro == "dentro" or op.ponto_encontro == 1.0
    lo, hi = (np.ceil, np.floor) if para_dentro else (np.floor, np.ceil)
    r1 = lambda f, x: round(float(f(x * 10 - 1e-6 * (1 if f is np.ceil else -1)) / 10), 1)   # noqa: E731
    campo.jaw_y = (r1(lo, jy[0]), r1(hi, jy[1]))
    campo.jaw_x = None if jx is None else (r1(lo, jx[0]), r1(hi, jx[1]))
    campo.mlc_a = [r1(lo, x) for x in a]
    campo.mlc_b = [r1(hi, x) for x in b]
    if para_dentro:                                          # o arredondamento não pode violar o gap mínimo
        for i in range(len(a)):
            if campo.mlc_b[i] - campo.mlc_a[i] < colim.gap_minimo_mm:
                campo.mlc_a[i], campo.mlc_b[i] = r1(np.floor, a[i]), r1(np.ceil, b[i])
    campo.alvo_conformado = nome_alvo
    campo.margem_mm = float(max(mg.maxima() for _, mg in alvos)) if alvos else None
    erros = verificar_mecanica(campo, colim)
    if erros:
        raise ValueError("; ".join(erros))
    return ResultadoAjuste(campo, abertura, avisos + av2, corr)


def otimizar_colimador(campo: Campo, colim: Colimacao, alvos: list[tuple], eixos: tuple,
                       angulos=range(0, 180, 5), op: OpcoesAjuste | None = None, sad_mm: float = 1000.0,
                       posicao: str = "HFS", blindagens: list[tuple] | None = None) -> tuple[float, dict]:
    """Ângulo de colimador que minimiza o erro de conformação (área exposta fora da abertura + área bloqueada da
    abertura), com desempate pelo ângulo mais próximo de 0/90. Não existe automático equivalente no Eclipse 3D."""
    op = op or OpcoesAjuste(resolucao_mm=1.0)
    custos = {}
    import copy
    for ang in angulos:
        c = copy.deepcopy(campo)
        c.colimador = float(ang)
        try:
            r = ajustar(c, colim, alvos, blindagens, eixos, op, sad_mm, posicao)
        except ValueError:
            continue
        aberto = raster_da_abertura(c, colim, r.abertura.u, r.abertura.v).m
        erro = (aberto & ~r.abertura.m).sum() + (r.abertura.m & ~aberto).sum()
        custos[float(ang)] = float(erro) * r.abertura.res ** 2 / 100.0      # cm²
    if not custos:
        raise ValueError("Nenhum ângulo de colimador viável")
    melhor = min(custos.values())
    candidatos = [a for a, e in custos.items() if e <= melhor * 1.02 + 0.05]
    escolhido = min(candidatos, key=lambda a: min(a % 90, 90 - a % 90))
    return escolhido, custos


def acomodar_lamina(a, b, colim: Colimacao, par: int, banco: str, pos: float) -> tuple[list, list]:
    """Move uma lâmina como no Eclipse: respeita gap, overtravel e extensão; num MLC sem interdigitação (MLCi)
    empurra as lâminas vizinhas em vez de recusar. Pares fechados vizinhos andam juntos (continuam fechados)."""
    a = np.asarray(a, float).copy()
    b = np.asarray(b, float).copy()
    g = colim.gap_minimo_mm
    lmax, ot = colim.lamina_max_mm, colim.overtravel_mm
    if banco == "a":
        a[par] = float(np.clip(pos, -lmax, min(ot, lmax - g)))
        if b[par] - a[par] < g:
            b[par] = a[par] + g
    else:
        b[par] = float(np.clip(pos, max(-ot, -lmax + g), lmax))
        if b[par] - a[par] < g:
            a[par] = b[par] - g
    if not getattr(colim, "interdigitacao", True):
        fila = [par]
        visitados = 0
        while fila and visitados < 8 * colim.n_pares:
            i = fila.pop(0)
            visitados += 1
            for j in (i - 1, i + 1):
                if not 0 <= j < colim.n_pares:
                    continue
                fechado = b[j] - a[j] <= g + 1e-6
                mudou = False
                if a[i] > b[j] - g + 1e-9:            # A deste par passou da B do vizinho
                    b[j] = a[i] + g
                    if fechado:
                        a[j] = b[j] - g
                    mudou = True
                if b[i] < a[j] + g - 1e-9:            # B deste par passou da A do vizinho
                    a[j] = b[i] - g
                    if fechado:
                        b[j] = a[j] + g
                    mudou = True
                if mudou:
                    if b[j] - a[j] < g:
                        b[j] = a[j] + g
                    fila.append(j)
    a = np.clip(a, -lmax, ot)
    b = np.clip(b, -ot, lmax)
    return [round(float(x), 1) for x in a], [round(float(x), 1) for x in b]


def raster_de_poligono(pontos, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Máscara BEV [nv, nu] de um polígono (u, v) em mm no plano do isocentro."""
    from matplotlib.path import Path as _P
    p = np.asarray(pontos, float)
    if len(p) < 3:
        raise ValueError("Desenhe pelo menos 3 pontos")
    U, V = np.meshgrid(u, v)
    return _P(p).contains_points(np.c_[U.ravel(), V.ravel()]).reshape(U.shape)


def laminas_para_desenho(campo: Campo, colim: Colimacao, pontos, modo: str = "abrir",
                         ponto_encontro: str | float = "fora", res_mm: float = 0.5) -> ResultadoAjuste:
    """Ferramenta "desenhar e as lâminas vão para a forma" (Eclipse: Fit to drawing).

    modo: "abrir" (a abertura passa a ser o desenho), "acrescentar" (abertura atual ∪ desenho),
          "bloquear" (abertura atual − desenho)."""
    import copy as _copy
    if modo not in ("abrir", "acrescentar", "bloquear"):
        raise ValueError(f"Modo desconhecido: {modo}")
    ext = colim.jaw_y_max_mm + 5
    u, v = grade_bev(ext, res_mm)
    m = raster_de_poligono(pontos, u, v)
    if modo != "abrir":
        atual = raster_da_abertura(campo, colim, u, v).m
        m = (atual | m) if modo == "acrescentar" else (atual & ~m)
    if not m.any():
        raise ValueError("A forma resultante não tem abertura")
    c = _copy.deepcopy(campo)
    op = OpcoesAjuste(ponto_encontro=ponto_encontro, resolucao_mm=res_mm)
    return ajustar(c, colim, [], [], None, op, abertura_base=RasterBEV(u, v, m))
