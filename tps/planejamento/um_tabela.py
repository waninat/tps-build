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
    eq_p = eq_ab * dist / sad_mm
    isq = ((tab.ssd_cal_mm + tab.dmax_mm) / dist) ** 2
    sc, sp = tab._f(tab.sc, eq_col), tab._f(tab.sp, eq_p)
    tmr = tab.tmr_em(eq_p, prof_rad)
    oar = tab.oar_em(np.hypot(u, v) * dist / sad_mm, prof_geo)
    d = tab.cgy_por_um * isq * sc * sp * tmr * oar
    return {"cgy_por_um": d, "eq_col_cm": eq_col, "eq_abertura_cm": eq_ab, "prof_geo_mm": prof_geo,
            "prof_rad_mm": prof_rad, "dist_mm": dist, "isq": isq, "sc": sc, "sp": sp, "tmr": tmr, "oar": oar}


def conferir_plano(plano, colim: Colimacao, tab: TabelasFeixe, doses_um: dict, grade, ponto_mm=None,
                   tolerancia_pct: float = 3.0) -> list[dict]:
    """Compara, campo a campo, a dose/UM do motor com a das tabelas no ponto (padrão: isocentro de cada campo)."""
    from tps.planejamento.calculo import valor_em_ponto
    out = []
    for c in plano.campos:
        p = np.asarray(ponto_mm if ponto_mm is not None else c.isocentro_mm, float)
        t = dose_por_um_tabela(c, colim, tab, p, grade.eta, (grade.x, grade.y, grade.z), posicao=plano.posicao_paciente)
        motor = valor_em_ponto(doses_um[c.nome], grade, p) * 100.0
        dif = 100 * (motor / t["cgy_por_um"] - 1) if t["cgy_por_um"] > 0 else float("nan")
        out.append({"campo": c.nome, "motor_cgy_um": motor, "tabela_cgy_um": t["cgy_por_um"], "dif_pct": dif,
                    "ok": abs(dif) <= tolerancia_pct, **{k: t[k] for k in ("eq_abertura_cm", "prof_rad_mm", "tmr")}})
    return out
