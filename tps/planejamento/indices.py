"""Índices de qualidade do plano: homogeneidade, conformidade e gradiente (importantes em SBRT/SRS). SRS-PLN-140.

Com TV = volume do alvo, PIV = volume (no corpo) coberto pela isodose de prescrição, TV_PIV = parte do alvo coberta:
- HI (ICRU 83) = (D2 − D98) / D50 no alvo; ideal 0.
- CI (RTOG) = PIV / TV; ideal 1 (aceitável 1–2 em SRS).
- Paddick CI = TV_PIV² / (TV · PIV); ideal 1 (combina cobertura e excesso de volume irradiado).
- GI (Paddick) = PIV50 / PIV (volume da isodose de 50% da prescrição ÷ da prescrição); quanto menor, mais rápida a queda.
- R50% (RTOG 0813/0915) = PIV50 / TV; os limites dependem do tamanho do alvo.
- D2cm (RTOG): dose máxima a 2 cm ou mais do alvo, em % da prescrição.
- Cobertura: V95%, V100% (% do alvo) e D95, D98 (Gy).
"""
from __future__ import annotations

import numpy as np
from scipy.ndimage import distance_transform_edt


def calcular(total: np.ndarray, alvo: np.ndarray, corpo: np.ndarray, rx_gy: float, voxel_cc: float,
             passo_mm: tuple | None = None) -> dict:
    if alvo is None or not alvo.any() or rx_gy <= 0:
        return {}
    d_alvo = np.sort(total[alvo])
    p = lambda q: float(np.percentile(d_alvo, q))          # noqa: E731
    tv = float(alvo.sum()) * voxel_cc
    piv_m = (total >= rx_gy) & corpo
    piv = float(piv_m.sum()) * voxel_cc
    tv_piv = float((piv_m & alvo).sum()) * voxel_cc
    piv50 = float(((total >= 0.5 * rx_gy) & corpo).sum()) * voxel_cc
    out = {
        "tv_cc": round(tv, 2), "piv_cc": round(piv, 2), "piv50_cc": round(piv50, 2),
        "hi": round((p(98) - p(2)) / p(50), 3) if p(50) > 0 else None,
        "ci_rtog": round(piv / tv, 3) if tv else None,
        "ci_paddick": round(tv_piv ** 2 / (tv * piv), 3) if tv and piv else None,
        "gi": round(piv50 / piv, 2) if piv else None,
        "r50": round(piv50 / tv, 2) if tv else None,
        "v95_pct": round(float((d_alvo >= 0.95 * rx_gy).mean() * 100), 2),
        "v100_pct": round(float((d_alvo >= rx_gy).mean() * 100), 2),
        "d95_gy": round(p(5), 3), "d98_gy": round(p(2), 3), "d2_gy": round(p(98), 3), "d50_gy": round(p(50), 3),
        "dmax_alvo_pct": round(float(d_alvo[-1] / rx_gy * 100), 1),
    }
    if passo_mm is not None:
        dist = distance_transform_edt(~alvo, sampling=passo_mm)
        longe = (dist >= 20.0) & corpo
        if longe.any():
            out["d2cm_pct"] = round(float(total[longe].max() / rx_gy * 100), 1)
    return out
