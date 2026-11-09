"""Leitura de RTDOSE DICOM (dose do TPS). SRS-IO-020.

Retorna a dose em Gy numa grade [z, y, x] com eixos em mm (coordenadas do paciente).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pydicom


@dataclass
class DoseVolume:
    dose_gy: np.ndarray         # [nz, ny, nx]
    x: np.ndarray               # mm
    y: np.ndarray
    z: np.ndarray
    summation: str              # PLAN / BEAM / FRACTION ...
    dose_type: str              # PHYSICAL / EFFECTIVE
    referenced_plan_uid: str | None
    frame_of_reference: str

    @property
    def axes_zyx(self):
        return (self.z, self.y, self.x)


def read_rtdose(path: str | Path) -> DoseVolume:
    ds = pydicom.dcmread(str(path), force=True)
    if getattr(ds, "Modality", "") != "RTDOSE":
        raise ValueError(f"Arquivo não é RTDOSE (Modality={getattr(ds, 'Modality', '?')})")
    iop = np.asarray(ds.ImageOrientationPatient, float)
    if not np.allclose(iop, [1, 0, 0, 0, 1, 0], atol=1e-4):
        raise NotImplementedError(f"Orientação de dose não suportada: {iop}")
    if not hasattr(ds, "file_meta") or "TransferSyntaxUID" not in ds.file_meta:
        # Monaco exporta sem preâmbulo/meta: o conjunto vem em Implicit VR Little Endian
        ds.file_meta = pydicom.dataset.FileMetaDataset()
        ds.file_meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian
    arr = ds.pixel_array.astype(np.float64) * float(ds.DoseGridScaling)
    if arr.ndim == 2:
        arr = arr[None]
    ipp = np.asarray(ds.ImagePositionPatient, float)
    ps = [float(v) for v in ds.PixelSpacing]
    offsets = np.asarray(ds.GridFrameOffsetVector, float)
    x = ipp[0] + np.arange(arr.shape[2]) * ps[1]
    y = ipp[1] + np.arange(arr.shape[1]) * ps[0]
    z = ipp[2] + offsets
    if np.any(np.diff(z) < 0):
        order = np.argsort(z)
        z, arr = z[order], arr[order]
    if str(getattr(ds, "DoseUnits", "GY")).upper() != "GY":
        raise ValueError(f"Unidade de dose não suportada: {ds.DoseUnits}")
    ref = None
    if "ReferencedRTPlanSequence" in ds:
        ref = str(ds.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID)
    return DoseVolume(arr, x, y, z, str(getattr(ds, "DoseSummationType", "")),
                      str(getattr(ds, "DoseType", "")), ref, str(getattr(ds, "FrameOfReferenceUID", "")))
