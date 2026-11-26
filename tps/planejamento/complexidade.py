"""Complexidade do plano e elegibilidade para CQ por cálculo (sem medida antes do tratamento). SRS-PLN-111.

Métricas por abertura (campo estático, subcampo, segmento ou intervalo de arco), ponderadas pela UM:
* área (MLC ∩ mandíbulas) e lado do quadrado equivalente (4A/P);
* fração de pares abertos estreitos (fendas);
* MCS (McNiven 2010: variabilidade das lâminas × variação da área);
* UM por Gy da fração;
* arcos: variação das UM por grau (constante no DCA) e deslocamento das lâminas por grau;
* conformação: quanto do alvo projetado no BEV fica bloqueado;
* conferência independente de UM (a maior diferença).

Os limites ficam em dados/criterios_cq_calculo.yaml (a clínica sobrescreve em <dados>/criterios_cq_calculo.yaml).
A decisão de não medir é da clínica: o TPS só mostra se o plano está dentro dos limites que ela definiu.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml

from tps import paths
from tps.planejamento import calculo
from tps.planejamento.conformacao import pontos_do_alvo, projetar_bev

PADRAO = Path(__file__).resolve().parents[2] / "dados" / "criterios_cq_calculo.yaml"


def criterios() -> tuple[dict, str]:
    for f in (paths.home() / "criterios_cq_calculo.yaml", PADRAO):
        if f.exists():
            return yaml.safe_load(f.read_text(encoding="utf-8")) or {}, str(f)
    return {}, ""


def _aberturas(plano, colim) -> list[dict]:
    """[{campo, gantry, jaw_x, jaw_y, mlc_a, mlc_b, mu, colimador, mesa, iso}]"""
    out = []
    for c in plano.campos:
        mu_c = float(c.mu or 0.0)
        base = {"campo": c.campo_pai or c.nome, "colimador": c.colimador, "mesa": c.mesa, "iso": c.isocentro_mm}
        if c.pontos_controle:
            for s in calculo.intervalos_arco(c):
                out.append({**base, "gantry": s["gantry"], "jaw_x": s["jaw_x"], "jaw_y": s["jaw_y"],
                            "mlc_a": s["mlc_a"], "mlc_b": s["mlc_b"], "mu": mu_c * s["peso"]})
        elif c.segmentos:
            for s in c.segmentos:
                out.append({**base, "gantry": c.gantry, "jaw_x": s.get("jaw_x"), "jaw_y": s["jaw_y"],
                            "mlc_a": s["mlc_a"], "mlc_b": s["mlc_b"], "mu": mu_c * float(s["peso"])})
        elif c.abertura_definida():
            out.append({**base, "gantry": c.gantry, "jaw_x": c.jaw_x, "jaw_y": c.jaw_y, "mlc_a": c.mlc_a,
                        "mlc_b": c.mlc_b, "mu": mu_c})
    return [a for a in out if a["mu"] > 1e-6]


def _geometria(ab: dict, lim: np.ndarray, gap_min: float, fenda_mm: float) -> dict:
    a, b = np.asarray(ab["mlc_a"], float), np.asarray(ab["mlc_b"], float)
    jy = ab["jaw_y"]
    jx = ab["jaw_x"] or (-1e9, 1e9)
    h = np.clip(np.minimum(lim[1:], jy[1]) - np.maximum(lim[:-1], jy[0]), 0, None)
    a2, b2 = np.maximum(a, jx[0]), np.minimum(b, jx[1])
    w = np.clip(b2 - a2, 0, None)
    aberto = (h > 0) & (b - a > gap_min + 0.5) & (w > 0)
    area = float(np.sum(w * h))
    idx = np.nonzero(aberto)[0]
    per = 0.0
    for k, i in enumerate(idx):
        per += 2 * h[i]
        prox = idx[k + 1] if k + 1 < len(idx) else None
        if prox is not None and prox == i + 1:
            per += abs(a2[i] - a2[i + 1]) + abs(b2[i] - b2[i + 1])
        else:
            per += w[i]
        if k == 0 or idx[k - 1] != i - 1:
            per += w[i]
    eq = 4 * area / per if per > 0 else 0.0
    fendas = int(np.sum(aberto & (b - a < fenda_mm)))
    # MCS: LSV por banco (McNiven 2010) — só nos pares abertos e contíguos
    lsv = 1.0
    if idx.size > 1:
        for banco in (a2[idx], b2[idx]):
            amp = banco.max() - banco.min()
            if amp > 0:
                lsv *= float(np.mean((amp - np.abs(np.diff(banco))) / amp))
    return {"area_cm2": area / 100.0, "lado_eq_cm": eq / 10.0, "n_abertos": int(idx.size), "fendas": fendas,
            "lsv": lsv, "larguras": w * aberto}


def avaliar(plano, colim, conferencia: list | None = None, alvo: np.ndarray | None = None, eixos=None,
            posicao: str = "HFS") -> dict:
    cr, arq = criterios()
    lim = np.asarray(colim.limites_laminas, float)
    fenda_mm = float(cr.get("fenda_mm", 10.0))
    abs_ = _aberturas(plano, colim)
    if not abs_:
        return {"elegivel": False, "itens": [], "metricas": {}, "criterios_arquivo": arq,
                "motivo": "Plano sem UM: calcule a dose"}
    geo = [_geometria(a, lim, colim.gap_minimo_mm, fenda_mm) for a in abs_]
    mu = np.array([a["mu"] for a in abs_])
    # MCS por campo: AAV = Σ larguras / Σ larguras máximas do campo; MCS = Σ(LSV·AAV·UM)/ΣUM
    mcs_num, campos_max = 0.0, {}
    for a, gm in zip(abs_, geo):
        campos_max[a["campo"]] = np.maximum(campos_max.get(a["campo"], 0), gm["larguras"])
    for a, gm in zip(abs_, geo):
        den = float(np.sum(campos_max[a["campo"]]))
        aav = float(np.sum(gm["larguras"])) / den if den > 0 else 0.0
        mcs_num += gm["lsv"] * aav * a["mu"]
    mcs = mcs_num / float(mu.sum())
    n_ab = sum(gm["n_abertos"] for gm in geo) or 1
    fendas_pct = 100.0 * sum(gm["fendas"] * m for gm, m in zip(geo, mu)) / max(
        sum(gm["n_abertos"] * m for gm, m in zip(geo, mu)), 1e-9)
    met = {"area_min_cm2": min(gm["area_cm2"] for gm in geo),
           "lado_eq_min_cm": min(gm["lado_eq_cm"] for gm in geo),
           "fendas_pct": fendas_pct, "mcs": mcs,
           "um_total": float(sum(float(c.mu or 0) for c in plano.campos)),
           "um_por_gy": float(sum(float(c.mu or 0) for c in plano.campos)) / plano.prescricao.dose_fracao_gy,
           "n_aberturas": len(abs_), "pares_abertos": n_ab}
    # arcos: UM por grau e deslocamento das lâminas
    cv, desl = 0.0, 0.0
    for c in plano.campos:
        pc = c.pontos_controle
        if not pc:
            continue
        ang = np.array([float(p["gantry"]) for p in pc])
        L = np.abs((np.diff(ang) + 180.0) % 360.0 - 180.0)
        dw = np.diff([float(p["mw"]) for p in pc])
        ok = L > 1e-6
        r = dw[ok] / L[ok]
        if r.size and r.mean() > 0:
            cv = max(cv, float(r.std() / r.mean() * 100))
        A = np.array([p["mlc_a"] for p in pc], float)
        B = np.array([p["mlc_b"] for p in pc], float)
        d = np.maximum(np.abs(np.diff(A, axis=0)), np.abs(np.diff(B, axis=0))).max(axis=1)[ok] / L[ok]
        desl = max(desl, float(d.max()) if d.size else 0.0)
    if any(c.pontos_controle for c in plano.campos):
        met["cv_um_grau_pct"] = cv
        met["desloc_lamina_mm_grau"] = desl
    # bloqueio do alvo no BEV (média ponderada pela UM)
    if alvo is not None and eixos is not None and alvo.any():
        from tps.planejamento.modelo import Campo
        pts = pontos_do_alvo(alvo, *eixos)
        if len(pts) > 6000:
            pts = pts[np.random.default_rng(0).choice(len(pts), 6000, replace=False)]
        bl = []
        for a in abs_:
            t = Campo("t", a["gantry"], a["colimador"], a["mesa"], a["iso"])
            u, v = projetar_bev(pts, t, 1000.0, posicao)
            i = np.clip(np.searchsorted(lim, v) - 1, 0, len(lim) - 2)
            ma, mb = np.asarray(a["mlc_a"])[i], np.asarray(a["mlc_b"])[i]
            jx = a["jaw_x"] or (-1e9, 1e9)
            dentro = (u >= ma) & (u <= mb) & (u >= jx[0]) & (u <= jx[1]) & (v >= a["jaw_y"][0]) & (v <= a["jaw_y"][1])
            bl.append(100.0 * (1 - dentro.mean()))
        met["alvo_bloqueado_pct"] = float(np.average(bl, weights=mu))
    conf = [k for k in (conferencia or []) if k.get("dif_pct") is not None]
    if conf:
        met["conferencia_max_pct"] = float(max(abs(float(k["dif_pct"])) for k in conf))

    itens = []

    def item(i, nome, valor, limite, ok, unidade=""):
        itens.append({"id": i, "nome": nome, "valor": None if valor is None else round(float(valor), 2),
                      "limite": limite, "ok": bool(ok), "unidade": unidade})
    tecs = list(cr.get("tecnicas", ["3D", "DCA"]))
    item("tecnica", "Técnica sem modulação de intensidade", None, ", ".join(tecs), plano.tecnica in tecs)
    item("area", "Menor abertura", met["area_min_cm2"], f"≥ {cr.get('area_min_cm2', 9):g} cm²",
         met["area_min_cm2"] >= float(cr.get("area_min_cm2", 9)), "cm²")
    item("lado_eq", "Menor quadrado equivalente", met["lado_eq_min_cm"], f"≥ {cr.get('lado_eq_min_cm', 3):g} cm",
         met["lado_eq_min_cm"] >= float(cr.get("lado_eq_min_cm", 3)), "cm")
    item("fendas", f"Pares abertos com menos de {fenda_mm:g} mm", met["fendas_pct"],
         f"≤ {cr.get('fendas_max_pct', 5):g}%", met["fendas_pct"] <= float(cr.get("fendas_max_pct", 5)), "%")
    item("mcs", "MCS (complexidade; 1 = campo aberto)", met["mcs"], f"≥ {cr.get('mcs_min', 0.4):g}",
         met["mcs"] >= float(cr.get("mcs_min", 0.4)))
    item("um_gy", "UM por Gy da fração", met["um_por_gy"], f"≤ {cr.get('um_por_gy_max', 250):g}",
         met["um_por_gy"] <= float(cr.get("um_por_gy_max", 250)), "UM/Gy")
    if "cv_um_grau_pct" in met:
        item("um_grau", "Variação das UM por grau (arco)", met["cv_um_grau_pct"], f"≤ {cr.get('cv_um_grau_max_pct', 1):g}%",
             met["cv_um_grau_pct"] <= float(cr.get("cv_um_grau_max_pct", 1)), "%")
        item("desloc", "Deslocamento das lâminas por grau (informativo: acima do limite o gantry desacelera)",
             met["desloc_lamina_mm_grau"], f"{cr.get('desloc_lamina_max_mm_grau', 3.3):g} mm/° sem desacelerar", True,
             "mm/°")
    if "alvo_bloqueado_pct" in met:
        item("bloqueio", "Alvo bloqueado no BEV (média)", met["alvo_bloqueado_pct"],
             f"≤ {cr.get('alvo_bloqueado_max_pct', 15):g}%",
             met["alvo_bloqueado_pct"] <= float(cr.get("alvo_bloqueado_max_pct", 15)), "%")
    if "conferencia_max_pct" in met:
        item("conferencia", "Conferência independente de UM (maior diferença)", met["conferencia_max_pct"],
             f"≤ {cr.get('conferencia_max_pct', 3):g}%",
             met["conferencia_max_pct"] <= float(cr.get("conferencia_max_pct", 3)), "%")
    else:
        item("conferencia", "Conferência independente de UM", None, "obrigatória", False)
    return {"elegivel": all(i["ok"] for i in itens), "itens": itens, "metricas": met, "criterios_arquivo": arq,
            "confirmado": bool(cr.get("confirmado_pela_clinica", False))}
