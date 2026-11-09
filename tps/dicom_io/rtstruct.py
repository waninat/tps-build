"""Leitura de RTSTRUCT e rasterização de estruturas numa grade [z, y, x]. SRS-IO-030..031.

Rasterização por corte: contornos fechados (CLOSED_PLANAR) no plano z mais próximo;
pontos dentro pela regra par-ímpar (furos e ilhas tratados por XOR entre contornos).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pydicom
from matplotlib.path import Path as MplPath


@dataclass
class Structure:
    number: int
    name: str
    rt_type: str                     # EXTERNAL, PTV, CTV, ORGAN, ...
    contours: list[np.ndarray]       # cada um [n, 3] em mm
    color: tuple | None = None       # ROIDisplayColor (RGB 0–255), como no TPS


def read_rtstruct(path: str | Path) -> list[Structure]:
    ds = pydicom.dcmread(str(path), force=True)
    if getattr(ds, "Modality", "") != "RTSTRUCT":
        raise ValueError("Arquivo não é RTSTRUCT")
    names = {int(r.ROINumber): str(r.ROIName) for r in ds.StructureSetROISequence}
    types = {int(o.ReferencedROINumber): str(getattr(o, "RTROIInterpretedType", ""))
             for o in getattr(ds, "RTROIObservationsSequence", [])}
    out = []
    for roi in getattr(ds, "ROIContourSequence", []):
        n = int(roi.ReferencedROINumber)
        col = tuple(int(c) for c in roi.ROIDisplayColor) if "ROIDisplayColor" in roi else None
        cs = []
        for c in getattr(roi, "ContourSequence", []):
            if str(getattr(c, "ContourGeometricType", "CLOSED_PLANAR")) not in ("CLOSED_PLANAR", ""):
                continue
            pts = np.asarray(c.ContourData, float).reshape(-1, 3)
            if len(pts) >= 3:
                cs.append(pts)
        out.append(Structure(n, names.get(n, f"ROI_{n}"), types.get(n, ""), cs, col))
    return out


def rasterize(s: Structure, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Máscara booleana [nz, ny, nx] da estrutura na grade (eixos em mm, centros de voxel).

    Os contornos de um mesmo plano do CT são combinados por XOR (buracos, ilhas). Cada corte da grade recebe o
    plano de contorno mais próximo, dentro de meio espaçamento (da grade ou dos contornos, o maior); cortes além
    do primeiro e do último plano contornados só recebem se estiverem a até meio espaçamento da grade.

    Até a 0.7.2, todos os contornos que caíam num mesmo corte da grade eram combinados por XOR: com CT mais fino
    que a grade de dose (ex.: CT 1,5 mm e grade 3 mm), dois planos quase iguais se anulavam e a estrutura sumia."""
    mask = np.zeros((len(z), len(y), len(x)), bool)
    if not s.contours:
        return mask
    X, Y = np.meshgrid(x, y)
    pts = np.column_stack([X.ravel(), Y.ravel()])
    planos: dict[float, np.ndarray] = {}
    for c in s.contours:
        zc = round(float(np.mean(c[:, 2])), 2)
        inside = MplPath(c[:, :2]).contains_points(pts).reshape(len(y), len(x))
        planos[zc] = planos[zc] ^ inside if zc in planos else inside
    zp = np.array(sorted(planos))
    dz = float(abs(np.median(np.diff(z)))) if len(z) > 1 else 1.0
    dp = float(np.median(np.diff(zp))) if len(zp) > 1 else dz
    tol = 0.51 * max(dz, dp)
    for k, zk in enumerate(z):
        if zk < zp[0] - 0.51 * dz or zk > zp[-1] + 0.51 * dz:
            continue
        q = int(np.argmin(np.abs(zp - zk)))
        if abs(zp[q] - zk) <= tol:
            mask[k] = planos[float(zp[q])]
    return mask


def dvh(dose: np.ndarray, mask: np.ndarray, voxel_cc: float, bin_gy: float = 0.01) -> dict:
    """DVH cumulativo e métricas usuais (D95, D50, D2, Dmean, Dmax, V(prescrição) via chamada)."""
    d = dose[mask]
    if d.size == 0:
        return {"volume_cc": 0.0}
    edges = np.arange(0, d.max() + 2 * bin_gy, bin_gy)
    hist, _ = np.histogram(d, edges)
    cum = hist[::-1].cumsum()[::-1] / d.size * 100
    return {
        "volume_cc": float(d.size * voxel_cc),
        "Dmean": float(d.mean()), "Dmax": float(d.max()), "Dmin": float(d.min()),
        "D2": float(np.percentile(d, 98)), "D50": float(np.percentile(d, 50)),
        "D95": float(np.percentile(d, 5)), "D98": float(np.percentile(d, 2)),
        "dose_gy": edges[:-1], "volume_pct": cum,
    }
