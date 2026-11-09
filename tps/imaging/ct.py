"""Leitura de série de CT DICOM para um volume 3D. SRS-IMG-001.

- Aceita arquivos sem preâmbulo DICOM (como os guardados pelo Monaco em DCMData/).
- Ordena por posição ao longo da normal do corte; exige espaçamento uniforme.
- Volume em HU, índices [z, y, x]; coordenadas do paciente (DICOM) em mm.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pydicom

CT_SOP = "1.2.840.10008.5.1.4.1.1.2"


@dataclass
class CTVolume:
    hu: np.ndarray            # [nz, ny, nx] float32
    origin: np.ndarray        # posição (x, y, z) do centro do voxel [0,0,0], mm
    spacing: np.ndarray       # (dx, dy, dz) mm
    frame_of_reference: str
    patient_position: str
    description: str = ""

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.hu.shape

    def axes(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        nz, ny, nx = self.hu.shape
        x = self.origin[0] + np.arange(nx) * self.spacing[0]
        y = self.origin[1] + np.arange(ny) * self.spacing[1]
        z = self.origin[2] + np.arange(nz) * self.spacing[2]
        return x, y, z


def read_ct_series(folder: str | Path, pattern: str = "*") -> CTVolume:
    files = [p for p in Path(folder).glob(pattern) if p.is_file()]
    slices = []
    for p in files:
        try:
            ds = pydicom.dcmread(str(p), force=True)
        except Exception:
            continue
        if str(getattr(ds, "SOPClassUID", "")) != CT_SOP and getattr(ds, "Modality", "") != "CT":
            continue
        slices.append(ds)
    if not slices:
        raise ValueError(f"Nenhum corte de CT em {folder}")

    iop = np.asarray(slices[0].ImageOrientationPatient, float)
    if not np.allclose(iop, [1, 0, 0, 0, 1, 0], atol=1e-4):
        raise NotImplementedError(f"Orientação de imagem não axial/HFS-like: {iop}")
    normal = np.cross(iop[:3], iop[3:])
    slices.sort(key=lambda d: float(np.dot(normal, np.asarray(d.ImagePositionPatient, float))))
    z = np.array([float(np.dot(normal, d.ImagePositionPatient)) for d in slices])
    dz = np.diff(z)
    if len(z) > 1 and (np.ptp(dz) > 0.01 or np.any(dz <= 0)):
        raise ValueError(f"Espaçamento entre cortes não uniforme: {np.unique(np.round(dz, 3))}")

    frs = {str(getattr(d, "FrameOfReferenceUID", "")) for d in slices}
    if len(frs) > 1:
        raise ValueError("Cortes com FrameOfReferenceUID diferentes")

    ref = slices[0]
    vol = np.empty((len(slices), int(ref.Rows), int(ref.Columns)), np.float32)
    for k, d in enumerate(slices):
        if not hasattr(d, "file_meta") or "TransferSyntaxUID" not in d.file_meta:
            d.file_meta = pydicom.dataset.FileMetaDataset()
            d.file_meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian
        arr = d.pixel_array.astype(np.float32)
        vol[k] = arr * float(getattr(d, "RescaleSlope", 1)) + float(getattr(d, "RescaleIntercept", 0))

    ps = [float(v) for v in ref.PixelSpacing]          # (linha, coluna) = (dy, dx)
    return CTVolume(
        hu=vol,
        origin=np.asarray(ref.ImagePositionPatient, float),
        spacing=np.array([ps[1], ps[0], dz[0] if len(dz) else float(ref.SliceThickness)]),
        frame_of_reference=frs.pop(),
        patient_position=str(getattr(ref, "PatientPosition", "")),
        description=f"{getattr(ref, 'Manufacturer', '')} {getattr(ref, 'ManufacturerModelName', '')}".strip(),
    )
