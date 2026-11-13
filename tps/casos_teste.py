"""Casos de teste em DICOM (CT + RTSTRUCT) para experimentar o TPS sem dado de paciente.

    python -m tps.casos_teste saida/

Gera:
  cilindro/            cilindro de água 16 cm com inserto ósseo e PTV cúbico (fantoma do SecDose)
  separacao_variavel/  "pescoço → tórax": separação AP de 12 a 26 cm, PTV na linha média e medula — caso clássico de
                       compensador/field-in-field sem cunha
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
from contourpy import contour_generator
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

CT_SOP = "1.2.840.10008.5.1.4.1.1.2"
RS_SOP = "1.2.840.10008.5.1.4.1.1.481.3"


def _meta(ds, sop):
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = sop
    ds.SOPClassUID = sop
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID


def exportar_volume(pasta: str | Path, hu: np.ndarray, x: np.ndarray, y: np.ndarray, z: np.ndarray,
                    estruturas: dict, paciente: tuple = ("TESTE^FANTOMA", "FANTOMA02"), descricao: str = ""):
    """hu [nz, ny, nx]; estruturas {nome: (máscara [nz,ny,nx], tipo, (r,g,b))}."""
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    estudo, serie, frame = generate_uid(), generate_uid(), generate_uid()
    hoje = dt.date.today().strftime("%Y%m%d")
    uids = []
    for k, zz in enumerate(z):
        ds = Dataset()
        _meta(ds, CT_SOP)
        ds.Modality, ds.PatientName, ds.PatientID, ds.PatientBirthDate, ds.PatientSex = "CT", *paciente, "", "O"
        ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = estudo, serie, frame
        ds.StudyDate = ds.SeriesDate = hoje
        ds.StudyID, ds.SeriesNumber, ds.InstanceNumber = "1", 1, k + 1
        ds.PatientPosition = "HFS"
        if descricao:
            ds.SeriesDescription = descricao[:64]
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.ImagePositionPatient = [float(x[0]), float(y[0]), float(zz)]
        ds.PixelSpacing = [float(y[1] - y[0]), float(x[1] - x[0])]
        ds.SliceThickness = float(z[1] - z[0])
        ds.Rows, ds.Columns = len(y), len(x)
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 16, 15, 1
        ds.RescaleIntercept, ds.RescaleSlope = 0, 1
        ds.PixelData = np.round(hu[k]).astype("<i2").tobytes()
        ds.save_as(pasta / f"CT{k + 1:04d}.dcm", enforce_file_format=True)
        uids.append(ds.SOPInstanceUID)
    rs = Dataset()
    _meta(rs, RS_SOP)
    rs.Modality, rs.PatientName, rs.PatientID, rs.PatientBirthDate, rs.PatientSex = "RTSTRUCT", *paciente, "", "O"
    rs.StudyInstanceUID, rs.SeriesInstanceUID = estudo, generate_uid()
    rs.StudyDate = hoje
    rs.StructureSetLabel, rs.StructureSetDate = "TESTE", hoje
    fr = Dataset()
    fr.FrameOfReferenceUID = frame
    rt_est = Dataset()
    rt_est.ReferencedSOPClassUID = "1.2.840.10008.3.1.2.3.1"
    rt_est.ReferencedSOPInstanceUID = estudo
    rt_ser = Dataset()
    rt_ser.SeriesInstanceUID = serie
    rt_ser.ContourImageSequence = Sequence([_ref_img(u) for u in uids])
    rt_est.RTReferencedSeriesSequence = Sequence([rt_ser])
    fr.RTReferencedStudySequence = Sequence([rt_est])
    rs.ReferencedFrameOfReferenceSequence = Sequence([fr])
    rs.StructureSetROISequence, rs.ROIContourSequence, rs.RTROIObservationsSequence = Sequence(), Sequence(), Sequence()
    for n, (nome, (masc, tipo, cor)) in enumerate(estruturas.items(), start=1):
        roi = Dataset()
        roi.ROINumber, roi.ReferencedFrameOfReferenceUID, roi.ROIName, roi.ROIGenerationAlgorithm = n, frame, nome, "MANUAL"
        rs.StructureSetROISequence.append(roi)
        rc = Dataset()
        rc.ReferencedROINumber, rc.ROIDisplayColor = n, list(cor)
        rc.ContourSequence = Sequence()
        for k, zz in enumerate(z):
            if not masc[k].any():
                continue
            cg = contour_generator(x, y, masc[k].astype(float))
            for linha in cg.lines(0.5):
                if len(linha) < 3:
                    continue
                c = Dataset()
                c.ContourGeometricType = "CLOSED_PLANAR"
                c.NumberOfContourPoints = len(linha)
                c.ContourData = [round(float(v), 2) for p in linha for v in (p[0], p[1], zz)]
                c.ContourImageSequence = Sequence([_ref_img(uids[k])])
                rc.ContourSequence.append(c)
        rs.ROIContourSequence.append(rc)
        ob = Dataset()
        ob.ObservationNumber, ob.ReferencedROINumber, ob.RTROIInterpretedType, ob.ROIInterpreter = n, n, tipo, ""
        rs.RTROIObservationsSequence.append(ob)
    rs.save_as(pasta / "RS.dcm", enforce_file_format=True)


def _ref_img(uid):
    d = Dataset()
    d.ReferencedSOPClassUID, d.ReferencedSOPInstanceUID = CT_SOP, uid
    return d


def separacao_variavel(pasta):
    x = np.arange(-180, 181, 2.0)
    y = np.arange(-160, 161, 2.0)
    z = np.arange(-120, 121, 4.0)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    sep = 120 + (Z + 120) / 240 * 140                       # 12 cm (pescoço, z = −120) a 26 cm (tórax)
    larg = 110 + (Z + 120) / 240 * 60
    corpo = (np.abs(Y) <= sep / 2) & (np.abs(X) <= larg) & ((X / larg) ** 2 + (Y / (sep / 2)) ** 4 <= 1.15)
    hu = np.where(corpo, 0.0, -1000.0)
    cy = sep / 2 - 32                                         # canal medular posterior, fora do PTV
    medula = ((X ** 2 + (Y - cy) ** 2) <= 6 ** 2) & corpo
    vertebra = ((X ** 2 + (Y - cy) ** 2) <= 15 ** 2) & ~medula & corpo
    hu = np.where(vertebra, 700.0, hu)
    ptv = (np.abs(X) <= 45) & (np.abs(Y + 15) <= 25) & (np.abs(Z) <= 90) & corpo
    exportar_volume(pasta, hu.astype(np.float32), x, y, z,
                    {"BODY": (corpo, "EXTERNAL", (0, 200, 0)), "PTV": (ptv, "PTV", (230, 25, 75)),
                     "MEDULA": (medula, "ORGAN", (0, 130, 200)), "VERTEBRA": (vertebra, "ORGAN", (220, 220, 120))},
                    ("TESTE^SEPARACAO", "FANTOMA03"))


def fantoma_agua(pasta, lado_mm: float = 400.0, prof_mm: float = 400.0, voxel_mm: float = 2.0):
    """Cubo de água para calcular perfis/PDD num TPS (ex.: Monaco) e comparar com o nosso modelo.
    Superfície anterior em y = 0; 5 cm de ar acima; isocentro sugerido em (0, 100, 0) para SSD 90 ou na superfície."""
    x = np.arange(-lado_mm / 2 - 20, lado_mm / 2 + 20 + 1e-6, voxel_mm)
    y = np.arange(-50.0, prof_mm + 20 + 1e-6, voxel_mm)
    z = np.arange(-lado_mm / 2 - 20, lado_mm / 2 + 20 + 1e-6, voxel_mm)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    agua = (np.abs(X) <= lado_mm / 2) & (np.abs(Z) <= lado_mm / 2) & (Y >= 0) & (Y <= prof_mm)
    hu = np.where(agua, 0.0, -1000.0).astype(np.float32)
    exportar_volume(pasta, hu, x, y, z, {"BODY": (agua, "EXTERNAL", (0, 200, 0))}, ("AGUA^FANTOMA", "AGUA40"))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = Path(args[0] if args else "casos_teste")
    from tps import fantoma
    fantoma.gerar_exportacao(out / "cilindro", calcular_dose=False)
    for f in ("RP.dcm", "RD.dcm"):
        (out / "cilindro" / f).unlink(missing_ok=True)
    separacao_variavel(out / "separacao_variavel")
    if "--agua" in sys.argv:
        fantoma_agua(out / "agua_40cm")
    print(f"Casos de teste em {out.resolve()}")


if __name__ == "__main__":
    main()
