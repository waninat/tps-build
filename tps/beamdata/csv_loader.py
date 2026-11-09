"""Carregamento e validação dos dados de comissionamento em CSV. SRS-BD-001..004.

Formato: um CSV "longo" por tipo de dado (ver templates/beamdata/).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

import numpy as np

REQUIRED = {
    "pdd": ["maquina", "energia", "campo_x_cm", "campo_y_cm", "ssd_cm", "profundidade_mm", "dose_rel"],
    "perfis": ["maquina", "energia", "campo_x_cm", "campo_y_cm", "ssd_cm", "profundidade_mm",
               "eixo", "posicao_mm", "dose_rel"],
    "fatores_output": ["maquina", "energia", "campo_x_cm", "campo_y_cm", "ssd_cm",
                       "profundidade_mm", "fator_output"],
    "calibracao": ["maquina", "energia", "ssd_cm", "profundidade_mm", "campo_x_cm", "campo_y_cm",
                   "cGy_por_MU"],
}
_NUM = {"campo_x_cm", "campo_y_cm", "ssd_cm", "profundidade_mm", "dose_rel", "posicao_mm",
        "fator_output", "cGy_por_MU"}


def _read(path: Path, kind: str) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(2048)
        fh.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        rows = list(csv.DictReader(fh, dialect=dialect))
    missing = [c for c in REQUIRED[kind] if c not in (rows[0].keys() if rows else [])]
    if missing:
        raise ValueError(f"{path.name}: colunas ausentes {missing}")
    out = []
    for i, r in enumerate(rows, start=2):
        rec = {}
        for k, v in r.items():
            if k in _NUM:
                try:
                    rec[k] = float(str(v).replace(",", "."))   # aceita vírgula decimal
                except ValueError as e:
                    raise ValueError(f"{path.name} linha {i}: '{k}'='{v}' não numérico") from e
            else:
                rec[k] = (v or "").strip()
        out.append(rec)
    return out


def load_curves(path: str | Path, kind: str) -> dict[tuple, dict[str, np.ndarray]]:
    """Agrupa pdd/perfis por (maquina, energia, campo, ssd[, prof, eixo]) -> arrays ordenados."""
    rows = _read(Path(path), kind)
    groups: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if kind == "pdd":
            key = (r["maquina"], r["energia"], r["campo_x_cm"], r["campo_y_cm"], r["ssd_cm"])
            groups[key].append((r["profundidade_mm"], r["dose_rel"]))
        else:
            key = (r["maquina"], r["energia"], r["campo_x_cm"], r["campo_y_cm"], r["ssd_cm"],
                   r["profundidade_mm"], r["eixo"])
            groups[key].append((r["posicao_mm"], r["dose_rel"]))
    out = {}
    for key, pts in groups.items():
        a = np.array(sorted(pts))
        out[key] = {"x": a[:, 0], "y": a[:, 1]}
    return out


def load_table(path: str | Path, kind: str) -> list[dict]:
    return _read(Path(path), kind)


def validate_pdd(curve: dict[str, np.ndarray]) -> list[str]:
    """Checagens básicas de qualidade da curva medida."""
    msgs = []
    x, y = curve["x"], curve["y"]
    if len(x) < 10:
        msgs.append("PDD com menos de 10 pontos")
    if np.any(np.diff(x) <= 0):
        msgs.append("Profundidades repetidas/não crescentes")
    if y.max() > 0 and abs(y.max() - 100) > 2:
        msgs.append(f"PDD não normalizado em 100 (máx={y.max():.1f})")
    return msgs
