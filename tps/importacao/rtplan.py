"""Importação de RTPLAN DICOM (ex.: plano 3D do Monaco) para um Plano editável do TPS. SRS-IMP-001.

Lê campos (gantry, colimador, mesa, isocentro, energia, mandíbulas, MLC), segmentos step-and-shoot (pares de pontos de
controle com peso de UM, como o Monaco grava o campo dentro do campo num único feixe STATIC), UM, prescrição (Dose
Reference TARGET), frações e pontos de referência. A dose do RTDOSE (PLAN) entra como dose de referência para
comparação, sem substituir o cálculo do TPS.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian

from tps.planejamento.modelo import Campo


def ler_dicom(f, sem_pixels: bool = False):
    """O Monaco grava sem preâmbulo/File Meta: deduz a sintaxe de transferência."""
    d = pydicom.dcmread(str(f), force=True, stop_before_pixels=sem_pixels)
    fm = getattr(d, "file_meta", None)
    if fm is None or "TransferSyntaxUID" not in fm:
        d.file_meta = fm or FileMetaDataset()
        d.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian if d.is_implicit_VR else ExplicitVRLittleEndian
    return d


def listar_planos(pasta: Path) -> list[dict]:
    """RTPLANs na pasta (com o RTDOSE que os referencia, se houver)."""
    planos, doses = [], {}
    for f in sorted(p for p in Path(pasta).iterdir() if p.is_file()):
        try:
            d = ler_dicom(f, sem_pixels=True)
        except Exception:  # noqa: BLE001
            continue
        mod = getattr(d, "Modality", "")
        if mod == "RTPLAN":
            planos.append((f, d))
        elif mod == "RTDOSE" and "ReferencedRTPlanSequence" in d:
            uid = str(d.ReferencedRTPlanSequence[0].ReferencedSOPInstanceUID)
            if str(getattr(d, "DoseSummationType", "")) == "PLAN" or uid not in doses:
                doses[uid] = f
    out = []
    for f, d in planos:
        feixes = [b for b in getattr(d, "BeamSequence", []) if getattr(b, "TreatmentDeliveryType", "TREATMENT")
                  == "TREATMENT"]
        out.append({"arquivo": f.name, "rotulo": str(getattr(d, "RTPlanLabel", "")),
                    "nome": str(getattr(d, "RTPlanName", "")), "campos": len(feixes),
                    "maquina": str(feixes[0].TreatmentMachineName) if feixes else "",
                    "fabricante": f"{getattr(d, 'Manufacturer', '')} {getattr(d, 'ManufacturerModelName', '')}".strip(),
                    "aprovado": str(getattr(d, "ApprovalStatus", "")),
                    "dose": doses.get(str(d.SOPInstanceUID)).name if str(d.SOPInstanceUID) in doses else None,
                    "frame_of_reference": str(getattr(d, "FrameOfReferenceUID", ""))})
    return out


def _posicoes(cp) -> dict:
    out = {}
    for b in getattr(cp, "BeamLimitingDevicePositionSequence", []):
        out[str(b.RTBeamLimitingDeviceType)] = [float(v) for v in b.LeafJawPositions]
    return out


def ler_plano(arquivo: Path) -> dict:
    ds = ler_dicom(arquivo)
    fg = ds.FractionGroupSequence[0]
    um = {int(r.ReferencedBeamNumber): float(r.BeamMeterset) for r in fg.ReferencedBeamSequence
          if "BeamMeterset" in r}
    dose_feixe = {int(r.ReferencedBeamNumber): float(r.BeamDose) for r in fg.ReferencedBeamSequence
                  if "BeamDose" in r and r.BeamDose is not None}
    fracoes = int(fg.NumberOfFractionsPlanned)
    dose_total, alvo_desc, pontos = None, "", []
    for r in getattr(ds, "DoseReferenceSequence", []):
        tipo = str(getattr(r, "DoseReferenceStructureType", ""))
        if tipo == "COORDINATES" and "DoseReferencePointCoordinates" in r:
            pontos.append({"nome": str(getattr(r, "DoseReferenceDescription", "") or f"REF{r.DoseReferenceNumber}"),
                           "xyz": [float(v) for v in r.DoseReferencePointCoordinates]})
        if str(getattr(r, "DoseReferenceType", "")) == "TARGET" and getattr(r, "TargetPrescriptionDose", None):
            if dose_total is None:
                dose_total = float(r.TargetPrescriptionDose)
                alvo_desc = str(getattr(r, "DoseReferenceDescription", ""))
    if dose_total is None and dose_feixe:
        dose_total = sum(dose_feixe.values()) * fracoes
    campos, avisos = [], []
    maquina = ""
    for b in ds.BeamSequence:
        if str(getattr(b, "TreatmentDeliveryType", "TREATMENT")) != "TREATMENT":
            avisos.append(f"Campo {b.BeamName}: tipo {b.TreatmentDeliveryType} (setup) ignorado")
            continue
        if str(getattr(b, "RadiationType", "PHOTON")) != "PHOTON":
            avisos.append(f"Campo {b.BeamName}: {b.RadiationType} não suportado nesta versão; ignorado")
            continue
        if int(getattr(b, "NumberOfWedges", 0) or 0):
            avisos.append(f"Campo {b.BeamName}: tem cunha — não suportada; importado SEM cunha")
        maquina = maquina or str(b.TreatmentMachineName)
        cps = list(b.ControlPointSequence)
        cp0 = cps[0]
        nome = str(getattr(b, "BeamName", "") or f"C{b.BeamNumber}")[:16]
        g = float(cp0.GantryAngle)
        col = float(getattr(cp0, "BeamLimitingDeviceAngle", 0.0) or 0.0)
        mesa = float(getattr(cp0, "PatientSupportAngle", 0.0) or 0.0)
        iso = tuple(float(v) for v in cp0.IsocenterPosition)
        e = float(getattr(cp0, "NominalBeamEnergy", 6.0) or 6.0)
        taxa = float(getattr(cp0, "DoseRateSet", 0) or 0) or 600.0
        ssd = float(cp0.SourceToSurfaceDistance) if getattr(cp0, "SourceToSurfaceDistance", None) else None
        # formas por ponto de controle (herdam a anterior quando não repetidas)
        atual = _posicoes(cp0)
        formas = []
        for cp in cps:
            novo = _posicoes(cp)
            if novo:
                atual = {**atual, **novo}
            formas.append((float(cp.CumulativeMetersetWeight), dict(atual)))
        final = float(getattr(b, "FinalCumulativeMetersetWeight", 1.0) or 1.0)
        segs = []
        for (w0, f0), (w1, _f1) in zip(formas[:-1], formas[1:]):
            if w1 - w0 > 1e-9:                         # step-and-shoot: a forma do início do intervalo é a entregue
                segs.append({"peso": (w1 - w0) / final, **_forma(f0)})
        if not segs:
            avisos.append(f"Campo {nome}: sem UM entregue; ignorado")
            continue
        c = Campo(nome=nome, gantry=g, colimador=col, mesa=mesa, isocentro_mm=iso, energia_mv=e, taxa_dose=taxa,
                  ssd_mm=ssd, mu=round(um.get(int(b.BeamNumber), 0.0), 1))
        principal = max(segs, key=lambda s: s["peso"])
        c.jaw_x, c.jaw_y, c.mlc_a, c.mlc_b = principal["jaw_x"], principal["jaw_y"], principal["mlc_a"], principal["mlc_b"]
        if len(segs) > 1:
            c.segmentos = [{k: (list(v) if isinstance(v, (list, tuple)) else v) for k, v in s.items()} for s in segs]
        c.descricao = str(getattr(b, "BeamDescription", "") or "")
        campos.append(c)
    return {"rotulo": str(getattr(ds, "RTPlanLabel", ""))[:16], "nome": str(getattr(ds, "RTPlanName", "")),
            "fracoes": fracoes, "dose_total": dose_total, "alvo_descricao": alvo_desc, "maquina": maquina,
            "campos": campos, "pontos": pontos, "avisos": avisos, "sop": str(ds.SOPInstanceUID),
            "frame_of_reference": str(getattr(ds, "FrameOfReferenceUID", "")),
            "rtstruct": str(ds.ReferencedStructureSetSequence[0].ReferencedSOPInstanceUID)
            if "ReferencedStructureSetSequence" in ds else None,
            "fabricante": f"{getattr(ds, 'Manufacturer', '')} {getattr(ds, 'ManufacturerModelName', '')}".strip(),
            "dose_feixe_gy": dose_feixe, "tolerancia": [str(t.ToleranceTableLabel) for t in
                                                        getattr(ds, "ToleranceTableSequence", [])]}


def _forma(pos: dict) -> dict:
    jx = pos.get("ASYMX") or pos.get("X")
    jy = pos.get("ASYMY") or pos.get("Y")
    mlc = pos.get("MLCX")
    if mlc is None:
        raise ValueError("Campo sem MLCX: só colimação por MLC é suportada")
    n = len(mlc) // 2
    return {"jaw_x": list(jx) if jx else None, "jaw_y": list(jy) if jy else [-200.0, 200.0],
            "mlc_a": list(mlc[:n]), "mlc_b": list(mlc[n:])}


def ler_dose(arquivo: Path):
    """RTDOSE → (dose Gy [z, y, x], (z, y, x) mm)."""
    d = ler_dicom(arquivo)
    arr = d.pixel_array.astype(np.float64) * float(d.DoseGridScaling)
    if arr.ndim == 2:
        arr = arr[None]
    x = float(d.ImagePositionPatient[0]) + np.arange(int(d.Columns)) * float(d.PixelSpacing[1])
    y = float(d.ImagePositionPatient[1]) + np.arange(int(d.Rows)) * float(d.PixelSpacing[0])
    z = float(d.ImagePositionPatient[2]) + np.asarray(d.GridFrameOffsetVector, float)
    o = np.asarray(d.ImageOrientationPatient, float)
    if not np.allclose(o, [1, 0, 0, 0, 1, 0], atol=1e-3):
        raise ValueError("RTDOSE com orientação diferente de axial HFS")
    if z[0] > z[-1]:
        arr, z = arr[::-1], z[::-1]
    return arr, (z, y, x), str(getattr(d, "DoseSummationType", ""))
