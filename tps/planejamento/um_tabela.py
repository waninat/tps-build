"""Cálculo independente de UM por tabelas (TMR, Sc, Sp), método clássico. SRS-PLN-027.

Para cada campo e um ponto de cálculo, estima a dose por UM com os dados medidos (sem o motor de dose) e compara com
o motor. Serve de checagem interna obrigatória em todo plano 2D/3D, como a "segunda conta" do físico.

  D/UM = k_cal · ((SSD_cal + dmax) / dist_ponto)² · Sc(eq_col) · Sp(eq_ab) · TMR(d_rad, eq_ab) · OAR(r, d)

  eq_col: quadrado equivalente da abertura do colimador (jaws; no Agility, envelope das lâminas em X)
  eq_ab : quadrado equivalente da abertura efetiva (MLC ∩ jaws) = 4·área/perímetro, projetada no ponto
  d_rad : profundidade radiológica (integral de η ao longo do raio) — com CT; em água, profundidade geométrica
  OAR   : razão fora do eixo pela tabela diagonal (aproximação conservadora para pontos fora do eixo)
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.ndimage import map_coordinates

from tps.engine.geometry import beam_frame
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo


@dataclass
class TabelasFeixe:
    campos: np.ndarray            # lados dos quadrados (cm)
    prof_mm: np.ndarray
    tmr: np.ndarray               # [campo, prof]
    sc: dict
    sp: dict
    oar: dict                     # prof_mm -> (raio_mm, oar)
    dmax_mm: float
    cgy_por_um: float
    ssd_cal_mm: float = 1000.0

    @classmethod
    def ler(cls, pasta: str | Path, cgy_por_um: float, dmax_mm: float = 15.0) -> "TabelasFeixe":
        pasta = Path(pasta)
        linhas = [r for r in csv.DictReader(open(pasta / "tmr.csv", encoding="utf-8"))
                  if r["campo_x_cm"] == r["campo_y_cm"]]
        campos = np.array(sorted({float(r["campo_x_cm"]) for r in linhas}))
        profs = np.array(sorted({float(r["profundidade_mm"]) for r in linhas}))
        t = np.full((len(campos), len(profs)), np.nan)
        for r in linhas:
            t[np.searchsorted(campos, float(r["campo_x_cm"])), np.searchsorted(profs, float(r["profundidade_mm"]))] = float(r["tmr"])
        for i in range(len(campos)):                          # preenche lacunas por interpolação na profundidade
            ok = ~np.isnan(t[i])
            t[i] = np.interp(profs, profs[ok], t[i, ok])
        sc, sp = {}, {}
        for r in csv.DictReader(open(pasta / "fatores_output.csv", encoding="utf-8")):
            f = float(r["campo_x_cm"])
            scp = float(r["fator_output"])
            s_c = float(r["sc"]) if r.get("sc") else None
            s_p = float(r["sp"]) if r.get("sp") else None
            if s_c is None and s_p:
                s_c = scp / s_p
            if s_p is None and s_c:
                s_p = scp / s_c
            sc[f], sp[f] = (s_c, s_p) if s_c else (scp, 1.0)
        oar = {}
        f_oar = pasta / "oar_diagonal.csv"
        if f_oar.exists():
            for r in csv.DictReader(open(f_oar, encoding="utf-8")):
                oar.setdefault(float(r["profundidade_mm"]), []).append((float(r["posicao_mm"]), float(r["dose_rel"]) / 100))
            oar = {k: tuple(np.array(v).T) for k, v in oar.items()}
        return cls(campos, profs, t, sc, sp, oar, dmax_mm, cgy_por_um)

    def _f(self, d: dict, eq: float) -> float:
        k = np.array(sorted(d))
        return float(np.interp(eq, k, [d[x] for x in k]))

    def tmr_em(self, eq_cm: float, prof_mm: float) -> float:
        por_campo = np.array([np.interp(prof_mm, self.prof_mm, self.tmr[i]) for i in range(len(self.campos))])
        return float(np.interp(eq_cm, self.campos, por_campo))

    def oar_em(self, raio_mm: float, prof_mm: float) -> float:
        if not self.oar or raio_mm < 1.0:
            return 1.0
        ps = np.array(sorted(self.oar))
        vals = [np.interp(raio_mm, *self.oar[p]) for p in ps]
        return float(np.interp(prof_mm, ps, vals))


def quadrados_equivalentes(c: Campo, colim: Colimacao) -> tuple[float, float]:
    """(eq do colimador, eq da abertura efetiva) em cm, no isocentro."""
    lim = colim.limites_laminas
    jy0, jy1 = c.jaw_y
    a, b = np.asarray(c.mlc_a), np.asarray(c.mlc_b)
    dentro = (lim[1:] > jy0) & (lim[:-1] < jy1) & (b - a > colim.gap_minimo_mm + 0.5)
    jx0, jx1 = c.jaw_x if c.jaw_x else (float(a[dentro].min()), float(b[dentro].max()))
    X, Y = (jx1 - jx0) / 10, (jy1 - jy0) / 10
    eq_col = 2 * X * Y / (X + Y)
    linhas = []                                            # (h, lo, hi) de cada par, de baixo para cima
    for i in range(colim.n_pares):
        h = max(0.0, min(lim[i + 1], jy1) - max(lim[i], jy0))
        lo, hi = max(a[i], jx0), min(b[i], jx1)
        linhas.append((h, lo, hi) if h > 0 and hi > lo else None)
    area = sum(h * (hi - lo) for h, lo, hi in filter(None, linhas))
    per = 0.0
    ant = None
    for ln in linhas + [None]:
        if ln is not None:
            h, lo, hi = ln
            per += 2 * h
            if ant is None:
                per += hi - lo
            elif min(hi, ant[2]) > max(lo, ant[1]):
                per += abs(lo - ant[1]) + abs(hi - ant[2])
            else:
                per += (hi - lo) + (ant[2] - ant[1])
        elif ant is not None:
            per += ant[2] - ant[1]
        ant = ln
    eq_ab = 4 * area / per / 10 if per > 0 else 0.0
    return eq_col, eq_ab


def profundidade_radiologica(eta: np.ndarray, eixos: tuple, origem: np.ndarray, ponto: np.ndarray,
                             passo_mm: float = 1.0) -> tuple[float, float]:
    """(profundidade geométrica, radiológica) do ponto ao longo do raio vindo da origem (fonte), em mm."""
    x, y, z = eixos
    d = ponto - origem
    L = np.linalg.norm(d)
    t = np.arange(0, L, passo_mm)
    pts = origem + np.outer(t / L, d)
    idx = [(pts[:, 2] - z[0]) / (z[1] - z[0]), (pts[:, 1] - y[0]) / (y[1] - y[0]), (pts[:, 0] - x[0]) / (x[1] - x[0])]
    e = map_coordinates(eta, idx, order=1, mode="constant", cval=0.0)
    dentro = np.nonzero(e > 0.5)[0]
    if len(dentro) == 0:
        return 0.0, 0.0
    geo = L - t[dentro[0]]
    return float(geo), float(np.sum(e[dentro[0]:]) * passo_mm)


def _abertura_iso(c: Campo, colim: Colimacao, res: float = 1.0):
    """Abertura do campo no plano do isocentro: (u, v, máscara[v, u]) — mandíbulas e lâminas."""
    lim = colim.limites_laminas
    jx = c.jaw_x if c.jaw_x else (float(np.nanmin(c.mlc_a)), float(np.nanmax(c.mlc_b)))
    u = np.arange(jx[0] + res / 2, jx[1], res)
    v = np.arange(c.jaw_y[0] + res / 2, c.jaw_y[1], res)
    i = np.clip(np.searchsorted(lim, v, side="right") - 1, 0, colim.n_pares - 1)
    a = np.asarray(c.mlc_a, float)[i]
    b = np.asarray(c.mlc_b, float)[i]
    m = (u[None, :] > a[:, None]) & (u[None, :] < b[:, None]) & ((b - a) > colim.gap_minimo_mm + 0.5)[:, None]
    return u, v, m


def clarkson(c: Campo, colim: Colimacao, tab: "TabelasFeixe", u0: float, v0: float, escala: float, prof_rad: float,
             n_setores: int = 36, passo: float = 1.0) -> tuple[float, float]:
    """Integração de Clarkson de Sp·TMR sobre a abertura vista do ponto (u0, v0) no plano do isocentro.
    Cada setor soma g(r_saída) − g(r_entrada) dos trechos abertos (g = Sp·TMR do círculo de raio r ↔ quadrado de
    lado 1,772·r), com r em cm no plano do ponto (× escala). Devolve (Sp·TMR efetivo, quadrado equivalente)."""
    u, v, m = _abertura_iso(c, colim)
    if not m.any():
        return 0.0, 0.0
    rmax = float(np.hypot(max(abs(u[0] - u0), abs(u[-1] - u0)), max(abs(v[0] - v0), abs(v[-1] - v0)))) + 2
    rs = np.arange(passo / 2, rmax, passo)
    eqs = np.unique(np.r_[np.linspace(0.5, 40, 80)])

    def g(r_mm):
        eq = np.clip(1.772 * r_mm * escala / 10.0, eqs[0], eqs[-1])
        return np.array([tab._f(tab.sp, e) * tab.tmr_em(e, prof_rad) for e in np.atleast_1d(eq)])
    du, dv = u[1] - u[0] if len(u) > 1 else 1.0, v[1] - v[0] if len(v) > 1 else 1.0
    tot = 0.0
    bordas = []
    for th in np.linspace(0, 2 * np.pi, n_setores, endpoint=False):
        pu, pv = u0 + rs * np.cos(th), v0 + rs * np.sin(th)
        iu = np.round((pu - u[0]) / du).astype(int)
        iv = np.round((pv - v[0]) / dv).astype(int)
        ok = (iu >= 0) & (iu < len(u)) & (iv >= 0) & (iv < len(v))
        ab = np.zeros(len(rs), bool)
        ab[ok] = m[iv[ok], iu[ok]]
        dif = np.diff(np.r_[0, ab.astype(int), 0])
        ent = np.where(dif == 1)[0]
        sai = np.where(dif == -1)[0]
        for e0, s0 in zip(ent, sai):
            r_in = 0.0 if e0 == 0 else rs[e0] - passo / 2
            r_out = rs[s0 - 1] + passo / 2
            bordas.append(r_out)
            tot += float(g(r_out)[0]) - (float(g(r_in)[0]) if r_in > 0 else 0.0)
    val = tot / n_setores
    # quadrado equivalente com o mesmo Sp·TMR (para o relatório)
    eqv = np.linspace(1.0, 40.0, 157)
    gv = np.array([tab._f(tab.sp, e) * tab.tmr_em(e, prof_rad) for e in eqv])
    eq = float(np.interp(val, gv, eqv)) if val > gv[0] else float(eqv[0])
    return val, eq


def dose_por_um_tabela(c: Campo, colim: Colimacao, tab: TabelasFeixe, ponto_mm, eta=None, eixos=None,
                       prof_mm: float | None = None, sad_mm: float = 1000.0, posicao: str = "HFS") -> dict:
    """Dose (cGy) por UM no ponto pelo método de tabelas."""
    fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, sad_mm, posicao)
    p = np.asarray(ponto_mm, float)
    r = p - fr.source
    dist = float(r @ fr.w)                                     # distância fonte → plano do ponto, no eixo
    u = sad_mm * (r @ fr.u) / dist
    v = sad_mm * (r @ fr.v) / dist
    if eta is not None:
        prof_geo, prof_rad = profundidade_radiologica(eta, eixos, fr.source, p)
    else:
        prof_geo = prof_rad = float(prof_mm)
    eq_col, eq_ab = quadrados_equivalentes(c, colim)
    isq = ((tab.ssd_cal_mm + tab.dmax_mm) / dist) ** 2
    sc = tab._f(tab.sc, eq_col)
    # espalhamento no paciente por Clarkson sobre a abertura real (campo conformado irregular, ponto fora do centro,
    # ponto coberto por lâminas) em vez do quadrado equivalente 4A/P
    sptmr, eq_p = clarkson(c, colim, tab, u, v, dist / sad_mm, prof_rad)
    sp = tab._f(tab.sp, eq_p)
    tmr = sptmr / sp if sp > 0 else 0.0
    oar = tab.oar_em(np.hypot(u, v) * dist / sad_mm, prof_geo)
    d = tab.cgy_por_um * isq * sc * sptmr * oar
    return {"cgy_por_um": d, "eq_col_cm": eq_col, "eq_abertura_cm": eq_ab, "prof_geo_mm": prof_geo,
            "prof_rad_mm": prof_rad, "dist_mm": dist, "isq": isq, "sc": sc, "sp": sp, "tmr": tmr, "oar": oar}


def _ponto_aberto(c: Campo, colim: Colimacao, u: float, v: float) -> bool:
    lim = colim.limites_laminas
    if not (c.jaw_y[0] <= v <= c.jaw_y[1]) or (c.jaw_x and not (c.jaw_x[0] <= u <= c.jaw_x[1])):
        return False
    i = int(np.searchsorted(lim, v, side="right") - 1)
    return 0 <= i < colim.n_pares and c.mlc_a[i] < u < c.mlc_b[i]


def dose_por_um_tabela_segmentos(c: Campo, colim: Colimacao, tab: TabelasFeixe, ponto_mm, eta=None, eixos=None,
                                 transmissao: float = 0.01, posicao: str = "HFS") -> dict:
    """Campo com segmentos (compensador): soma, com os pesos, a dose/UM de cada segmento que deixa o ponto
    descoberto; segmentos que cobrem o ponto contribuem só com a transmissão do MLC."""
    total, eq = 0.0, []
    for s in c.segmentos:
        sc = Campo(c.nome, c.gantry, c.colimador, c.mesa, c.isocentro_mm, jaw_x=s.get("jaw_x"), jaw_y=s["jaw_y"],
                   mlc_a=s["mlc_a"], mlc_b=s["mlc_b"])
        t = dose_por_um_tabela(sc, colim, tab, ponto_mm, eta, eixos, posicao=posicao)
        fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, 1000.0, posicao)
        r = np.asarray(ponto_mm, float) - fr.source
        u, v = 1000.0 * (r @ fr.u) / (r @ fr.w), 1000.0 * (r @ fr.v) / (r @ fr.w)
        extra = 0.0
        if not _ponto_aberto(sc, colim, u, v):
            # ponto sob as lâminas: Clarkson já dá o espalhamento da parte aberta; soma a transmissão do primário
            prim = tab._f(tab.sp, 0.5) * tab.tmr_em(0.5, t["prof_rad_mm"])
            extra = transmissao * tab.cgy_por_um * t["isq"] * t["sc"] * prim * t["oar"]
        total += s["peso"] * (t["cgy_por_um"] + extra)
        eq.append(t["eq_abertura_cm"])
    t["cgy_por_um"], t["eq_abertura_cm"] = total, float(np.mean(eq))
    return t


def conferir_plano(plano, colim: Colimacao, tab: TabelasFeixe, doses_um: dict, grade, ponto_mm=None,
                   tolerancia_pct: float = 3.0) -> list[dict]:
    """Compara, campo a campo, a dose/UM do motor com a das tabelas no ponto (padrão: isocentro de cada campo)."""
    from tps.planejamento.calculo import valor_em_ponto
    out = []
    for c in plano.campos:
        p = np.asarray(ponto_mm if ponto_mm is not None else c.isocentro_mm, float)
        if c.segmentos:
            t = dose_por_um_tabela_segmentos(c, colim, tab, p, grade.eta, (grade.x, grade.y, grade.z),
                                             posicao=plano.posicao_paciente)
        else:
            t = dose_por_um_tabela(c, colim, tab, p, grade.eta, (grade.x, grade.y, grade.z),
                                   posicao=plano.posicao_paciente)
        motor = valor_em_ponto(doses_um[c.nome], grade, p) * 100.0
        dif = 100 * (motor / t["cgy_por_um"] - 1) if t["cgy_por_um"] > 0 else float("nan")
        out.append({"campo": c.nome, "motor_cgy_um": float(motor), "tabela_cgy_um": float(t["cgy_por_um"]),
                    "dif_pct": float(dif), "ok": bool(abs(dif) <= tolerancia_pct),
                    **{k: float(t[k]) for k in ("eq_abertura_cm", "prof_rad_mm", "tmr")}})
    # field-in-field: o campo e os seus subcampos são entregues como UM feixe (step-and-shoot); a conferência que
    # vale é a do feixe inteiro (Σ UM·dose/UM). Subcampo sozinho, com o ponto coberto pelas lâminas, não tem
    # conferência por tabela que faça sentido: fica como informativo.
    por_nome = {k["campo"]: k for k in out}
    for c in plano.campos:
        filhos = [f for f in plano.campos if f.campo_pai == c.nome]
        if not filhos or c.campo_pai:
            continue
        grupo = [c] + filhos
        mu = {f.nome: float(f.mu or 0.0) for f in grupo}
        if sum(mu.values()) <= 0:
            continue
        mot = sum(por_nome[f.nome]["motor_cgy_um"] * mu[f.nome] for f in grupo)
        tab_ = sum(por_nome[f.nome]["tabela_cgy_um"] * mu[f.nome] for f in grupo)
        dif = 100 * (mot / tab_ - 1) if tab_ > 0 else float("nan")
        k = por_nome[c.nome]
        k.update({"dif_pct": float(dif), "ok": bool(abs(dif) <= tolerancia_pct), "feixe": True,
                  "motor_cgy_um": float(mot / sum(mu.values())), "tabela_cgy_um": float(tab_ / sum(mu.values())),
                  "subcampos": [f.nome for f in filhos]})
        for f in filhos:
            por_nome[f.nome].update({"ok": True, "informativo": True})
    return out
