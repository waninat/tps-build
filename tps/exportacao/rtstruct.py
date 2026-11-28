"""Exportação do conjunto de estruturas (RTSTRUCT) com as estruturas do TPS: originais, editadas, novas e derivadas.
SRS-EXP-030. Os polígonos ficam nos planos do CT e referenciam o SOPInstanceUID de cada corte (exigência do
Monaco e do Mosaiq para associar o contorno à imagem).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from tps.version import __version__

RTSTRUCT_SOP = "1.2.840.10008.5.1.4.1.1.481.3"
CT_SOP = "1.2.840.10008.5.1.4.1.1.2"


def _ds(**kw) -> Dataset:
    d = Dataset()
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def cortes_do_ct(pasta: Path) -> list[tuple[float, str, str, str]]:
    """[(z, SOPInstanceUID, SeriesInstanceUID, SOPClassUID)] dos cortes de CT da pasta."""
    out = []
    for f in Path(pasta).iterdir():
        if not f.is_file():
            continue
        try:
            d = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
        except Exception:  # noqa: BLE001
            continue
        if getattr(d, "Modality", "") == "CT" and "ImagePositionPatient" in d:
            out.append((float(d.ImagePositionPatient[2]), str(d.SOPInstanceUID), str(d.SeriesInstanceUID),
                        str(getattr(d, "SOPClassUID", CT_SOP))))
    return sorted(out)


def exportar_rtstruct(sessao, destino: str | Path, rotulo: str = "TPS") -> Dataset:
    from tps.planejamento.contorno import poligonos_de_mascara
    s = sessao
    cortes = cortes_do_ct(s.pasta_ct)
    if not cortes:
        raise ValueError("Não encontrei os cortes do CT para referenciar o RTSTRUCT")
    zs = np.array([c[0] for c in cortes])
    ref_ct = pydicom.dcmread(str(next(f for f in s.pasta_ct.iterdir() if f.is_file() and s._e_ct(f))),
                             stop_before_pixels=True, force=True)
    agora = dt.datetime.now()
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = RTSTRUCT_SOP
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = RTSTRUCT_SOP
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.InstanceCreationDate, ds.InstanceCreationTime = agora.strftime("%Y%m%d"), agora.strftime("%H%M%S")
    for k in ("PatientName", "PatientID", "PatientBirthDate", "PatientSex", "StudyInstanceUID", "StudyID",
              "StudyDate", "StudyTime", "AccessionNumber", "ReferringPhysicianName"):
        setattr(ds, k, getattr(ref_ct, k, ""))
    ds.Modality = "RTSTRUCT"
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = 1
    ds.Manufacturer = "TPS (nome provisorio)"
    ds.ManufacturerModelName = "TPS"
    ds.SoftwareVersions = __version__
    ds.StructureSetLabel = rotulo[:16]
    ds.StructureSetName = rotulo[:64]
    ds.StructureSetDate, ds.StructureSetTime = agora.strftime("%Y%m%d"), agora.strftime("%H%M%S")
    fr = s.ct.frame_of_reference
    serie = cortes[0][2]
    ds.ReferencedFrameOfReferenceSequence = Sequence([_ds(
        FrameOfReferenceUID=fr,
        RTReferencedStudySequence=Sequence([_ds(
            ReferencedSOPClassUID="1.2.840.10008.3.1.2.3.1",
            ReferencedSOPInstanceUID=str(getattr(ref_ct, "StudyInstanceUID", "")),
            RTReferencedSeriesSequence=Sequence([_ds(
                SeriesInstanceUID=serie,
                ContourImageSequence=Sequence([_ds(ReferencedSOPClassUID=c[3], ReferencedSOPInstanceUID=c[1])
                                               for c in cortes]))]))]))])
    rois, contornos, obs = [], [], []
    cx, cy, _cz = s.ct.axes()
    g = s.grade
    tol = float(s.ct.spacing[2]) / 2 + 0.01
    for n, (nome, e) in enumerate(s.estruturas.items(), start=1):
        polys = list(e.contornos or [])
        if not polys and e.mascara.any():
            # estrutura só com máscara (derivada): polígonos em cada corte do CT a partir da grade de dose
            xs = np.arange(g.x[0], g.x[-1] + 1e-6, min(1.0, float(g.x[1] - g.x[0]) / 2))
            ys = np.arange(g.y[0], g.y[-1] + 1e-6, min(1.0, float(g.y[1] - g.y[0]) / 2))
            from scipy.interpolate import RegularGridInterpolator
            it = RegularGridInterpolator((g.z, g.y, g.x), e.mascara.astype(np.float32), bounds_error=False,
                                         fill_value=0.0)
            Y, X = np.meshgrid(ys, xs, indexing="ij")
            for z in zs:
                if z < g.z[0] - tol or z > g.z[-1] + tol:
                    continue
                f = it(np.stack([np.full_like(X, z), Y, X], -1)) >= 0.5
                polys += poligonos_de_mascara(f, xs, ys, z)
        if not polys:
            continue
        rois.append(_ds(ROINumber=n, ReferencedFrameOfReferenceUID=fr, ROIName=nome[:64],
                        ROIGenerationAlgorithm="MANUAL" if e.tipo not in ("DERIVADA",) else "SEMIAUTOMATIC"))
        seq = []
        for c in polys:
            c = np.asarray(c, float)
            k = int(np.argmin(np.abs(zs - float(c[0, 2]))))
            item = _ds(ContourGeometricType="CLOSED_PLANAR", NumberOfContourPoints=len(c),
                       ContourData=[round(float(v), 2) for v in c[:, :3].ravel()])
            if abs(zs[k] - float(c[0, 2])) <= tol:
                item.ContourImageSequence = Sequence([_ds(ReferencedSOPClassUID=cortes[k][3],
                                                          ReferencedSOPInstanceUID=cortes[k][1])])
            seq.append(item)
        contornos.append(_ds(ROIDisplayColor=[int(v) for v in e.cor][:3], ReferencedROINumber=n,
                             ContourSequence=Sequence(seq)))
        tipo = (e.tipo or "ORGAN").upper()
        if tipo in ("DERIVADA", "NOVA", "BOLUS", ""):
            tipo = "BOLUS" if e.tipo == "BOLUS" else ("PTV" if nome.upper().startswith("PTV") else "ORGAN")
        o = _ds(ObservationNumber=n, ReferencedROINumber=n, RTROIInterpretedType=tipo, ROIInterpreter="")
        dens = (getattr(s.plano, "densidades", None) or {}).get(nome) if s.plano else None
        if dens is not None:
            o.ROIPhysicalPropertiesSequence = Sequence([_ds(ROIPhysicalProperty="REL_ELEC_DENSITY",
                                                            ROIPhysicalPropertyValue=float(
                                                                dens["valor"] if isinstance(dens, dict) else dens))])
        obs.append(o)
    ds.StructureSetROISequence = Sequence(rois)
    ds.ROIContourSequence = Sequence(contornos)
    ds.RTROIObservationsSequence = Sequence(obs)
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(destino, enforce_file_format=True)
    return ds
