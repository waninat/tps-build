"""Métricas de complexidade do plano (a partir do RTPLAN). SRS-CPX-001..003.

- MU/Gy: MU por fração dividida pela dose por fração.
- Área média da abertura (cm²), ponderada por MU.
- SAS5 / SAS10 (Crowe et al. 2014): fração dos pares de lâminas abertos com abertura < 5 / 10 mm.
- MCS (McNiven et al. 2010): LSV × AAV, ponderado por MU; 1 = campo aberto regular, → 0 = muito modulado.
- Deslocamento médio das lâminas por feixe (mm) e MU por grau (VMAT).
"""
from __future__ import annotations

import numpy as np

from tps.dicom_io.rtplan import Plan


def _segments(beam):
    cps = beam.control_points
    out = []
    for k in range(len(cps) - 1):
        c0, c1 = cps[k], cps[k + 1]
        mu = c0.mu_segment
        if mu <= 1e-9 or c0.mlc_a is None:
            continue
        dyn = beam.technique in ("VMAT", "DMLC")
        a = 0.5 * (c0.mlc_a + c1.mlc_a) if dyn else c0.mlc_a
        b = 0.5 * (c0.mlc_b + c1.mlc_b) if dyn else c0.mlc_b
        jy = c0.jaw_y if not dyn or c1.jaw_y is None else tuple(0.5 * (np.array(c0.jaw_y) + np.array(c1.jaw_y)))
        jx = c0.jaw_x
        out.append((mu, a, b, jy, jx, abs(((c1.gantry - c0.gantry + 180) % 360) - 180)))
    return out


def _open_pairs(a, b, jy, jx, bounds):
    lo, hi = bounds[:-1], bounds[1:]
    width = hi - lo
    if jy is not None:
        overlap = np.clip(np.minimum(hi, jy[1]) - np.maximum(lo, jy[0]), 0, None)
    else:
        overlap = width.copy()
    a2, b2 = a.copy(), b.copy()
    if jx is not None:
        a2, b2 = np.maximum(a2, jx[0]), np.minimum(b2, jx[1])
    gap = np.clip(b2 - a2, 0, None)
    open_ = (overlap > 0) & (gap > 0.5)          # par efetivamente aberto dentro dos diafragmas
    return gap, overlap, open_


def plan_complexity(plan: Plan) -> dict:
    tot_mu = area = sas5 = sas10 = mcs = 0.0
    travel, deg = [], 0.0
    for beam in plan.beams:
        if beam.leaf_boundaries is None:
            continue
        segs = _segments(beam)
        if not segs:
            continue
        bounds = np.asarray(beam.leaf_boundaries, float)
        max_gap = np.zeros(len(bounds) - 1)
        per_seg = []
        for mu, a, b, jy, jx, dg in segs:
            gap, ov, op = _open_pairs(np.asarray(a), np.asarray(b), jy, jx, bounds)
            max_gap = np.maximum(max_gap, np.where(op, gap, 0))
            per_seg.append((mu, a, b, gap, ov, op))
            deg += dg
        mu_b = sum(s[0] for s in per_seg)
        for mu, a, b, gap, ov, op in per_seg:
            w = mu
            tot_mu += w
            area += w * float(np.sum(gap * ov)) / 100.0
            n_open = int(op.sum())
            if n_open:
                sas5 += w * float(np.mean(gap[op] < 5.0))
                sas10 += w * float(np.mean(gap[op] < 10.0))
                lsv = 1.0
                for bank in (np.asarray(a)[op], np.asarray(b)[op]):
                    if len(bank) < 2:
                        continue
                    rng = bank.max() - bank.min()
                    lsv *= 1.0 if rng <= 0 else float(np.mean((rng - np.abs(np.diff(bank))) / rng))
                aav = float(gap[op].sum() / max(max_gap[op].sum(), 1e-9))
                mcs += w * lsv * aav
        cps = [c for c in beam.control_points if c.mlc_a is not None]
        if len(cps) > 1:
            A = np.array([c.mlc_a for c in cps]); B = np.array([c.mlc_b for c in cps])
            travel.append(float(np.mean(np.abs(np.diff(A, axis=0)).sum(axis=0) + np.abs(np.diff(B, axis=0)).sum(axis=0)) / 2))
    if tot_mu <= 0:
        return {}
    presc = next((p["target_prescription_dose_gy"] for p in plan.prescription if p.get("target_prescription_dose_gy")), None)
    dose_fx = presc / plan.n_fractions if presc and plan.n_fractions else None
    return {
        "mu_por_gy": round(plan.total_mu / dose_fx, 1) if dose_fx else None,
        "area_media_cm2": round(area / tot_mu, 1),
        "sas5_pct": round(100 * sas5 / tot_mu, 1),
        "sas10_pct": round(100 * sas10 / tot_mu, 1),
        "mcs": round(mcs / tot_mu, 3),
        "deslocamento_lamina_mm": round(float(np.mean(travel)), 0) if travel else None,
        "mu_por_grau": round(plan.total_mu / deg, 2) if deg > 1 else None,
    }


DESCRICOES = {
    "mu_por_gy": ("MU/Gy", "MU por fração dividida pela dose por fração. Mais alto = mais modulado."),
    "mcs": ("MCS", "Modulation complexity score (0–1). Mais baixo = mais complexo."),
    "sas10_pct": ("Aberturas < 10 mm", "Fração dos pares abertos com abertura menor que 10 mm, ponderada por MU."),
    "sas5_pct": ("Aberturas < 5 mm", "Fração dos pares abertos com abertura menor que 5 mm, ponderada por MU."),
    "area_media_cm2": ("Área média", "Área média das aberturas, ponderada por MU (cm²)."),
    "deslocamento_lamina_mm": ("Percurso das lâminas", "Deslocamento médio de cada lâmina por feixe (mm)."),
    "mu_por_grau": ("MU por grau", "MU entregue por grau de rotação (VMAT)."),
}
# sentido em que o valor indica maior complexidade (para o percentil)
MAIS_COMPLEXO_SE_MAIOR = {"mu_por_gy": True, "mcs": False, "sas10_pct": True, "sas5_pct": True,
                          "area_media_cm2": False, "deslocamento_lamina_mm": True, "mu_por_grau": False}
