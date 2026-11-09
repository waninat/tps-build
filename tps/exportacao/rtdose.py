"""Exportação da dose calculada para DICOM RT Dose. SRS-EXP-030..033."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from tps.exportacao.rtplan import FABRICANTE, RTPLAN_SOP
from tps.version import __version__

RTDOSE_SOP = "1.2.840.10008.5.1.4.1.1.481.2"


def exportar_rtdose(dose_gy: np.ndarray, x: np.ndarray, y: np.ndarray, z: np.ndarray, rtplan: Dataset,
                    destino: str | Path, soma: str = "PLAN") -> Dataset:
    """dose_gy [nz, ny, nx] em Gy (soma do plano: todas as frações). Mesma referência e paciente do RTPLAN."""
    if dose_gy.shape != (len(z), len(y), len(x)):
        raise ValueError("Dimensões da dose não batem com os eixos")
    if not np.all(np.isfinite(dose_gy)) or dose_gy.min() < 0:
        raise ValueError("Dose com valor negativo ou não finito")
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = RTDOSE_SOP
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = RTDOSE_SOP
    for k in ("SpecificCharacterSet", "PatientName", "PatientID", "PatientBirthDate", "PatientSex",
              "StudyInstanceUID", "StudyID", "StudyDate", "StudyTime", "AccessionNumber", "ReferringPhysicianName",
              "FrameOfReferenceUID", "PositionReferenceIndicator"):
        setattr(ds, k, getattr(rtplan, k, ""))
    ds.Modality = "RTDOSE"
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = 2
    ds.InstanceNumber = 1
    ds.Manufacturer, ds.SoftwareVersions = FABRICANTE, __version__
    ds.DoseUnits, ds.DoseType, ds.DoseSummationType = "GY", "PHYSICAL", soma
    ds.ReferencedRTPlanSequence = Sequence([Dataset()])
    ds.ReferencedRTPlanSequence[0].ReferencedSOPClassUID = RTPLAN_SOP
    ds.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID = rtplan.SOPInstanceUID
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [float(x[0]), float(y[0]), float(z[0])]
    ds.PixelSpacing = [float(y[1] - y[0]), float(x[1] - x[0])]      # (linha, coluna)
    ds.SliceThickness = float(z[1] - z[0]) if len(z) > 1 else 0.0
    ds.GridFrameOffsetVector = [float(v) for v in z - z[0]]
    ds.FrameIncrementPointer = 0x3004000C
    ds.Rows, ds.Columns, ds.NumberOfFrames = len(y), len(x), len(z)
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 32, 32, 31, 0
    # margem abaixo de 2³²−1: o arredondamento do DS (16 caracteres) não pode estourar o inteiro
    esc = float(dose_gy.max()) / 4.0e9 if dose_gy.max() > 0 else 1.0
    ds.DoseGridScaling = f"{esc:.10g}"
    esc = float(ds.DoseGridScaling)
    px = np.round(dose_gy / esc)
    if px.max() > 2 ** 32 - 1:
        raise OverflowError("Escala do RTDOSE estourou o inteiro de 32 bits")
    ds.PixelData = px.astype("<u4").tobytes()
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(destino, enforce_file_format=True)
    return ds
