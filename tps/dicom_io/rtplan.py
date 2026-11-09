"""Leitor de DICOM RT Plan (foco inicial: Elekta + Monaco).

Requisitos atendidos: SRS-IO-001..SRS-IO-006 (ver docs/requisitos.md).

Particularidades tratadas:
- Pontos de controle (CP) só repetem atributos que mudaram -> herança do CP anterior.
- Elekta Precise (MLCi/MLCi2): ASYMX/X (backup jaws) + ASYMY/Y + MLCX.
- Elekta Agility: sem colimador X; dispositivos ASYMY/Y + MLCX.
- MU por CP derivada do CumulativeMetersetWeight normalizado pelo
  FinalCumulativeMetersetWeight e do BeamMeterset do FractionGroup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pydicom

_JAW_X = {"X", "ASYMX"}
_JAW_Y = {"Y", "ASYMY"}
_MLC = {"MLCX", "MLCY"}


@dataclass
class ControlPoint:
    index: int
    cumulative_weight: float          # normalizado 0..1
    gantry: float
    gantry_dir: str                   # CW / CC / NONE
    collimator: float
    couch: float
    energy_mv: float | None
    dose_rate: float | None
    jaw_x: tuple[float, float] | None  # mm no isocentro (None = sem colimador X)
    jaw_y: tuple[float, float] | None
    mlc_axis: str | None              # "MLCX" ou "MLCY"
    mlc_a: np.ndarray | None          # banco A (negativo), mm
    mlc_b: np.ndarray | None          # banco B (positivo), mm
    isocenter: tuple[float, float, float]
    ssd: float | None = None
    mu_segment: float = 0.0           # MU entregue entre este CP e o próximo


@dataclass
class Beam:
    number: int
    name: str
    description: str
    machine: str
    radiation_type: str
    beam_type: str                    # STATIC / DYNAMIC
    treatment_delivery: str
    technique: str                    # 3D, STEP&SHOOT, VMAT, DMLC (inferido)
    mu: float
    leaf_boundaries: np.ndarray | None
    sad: float
    control_points: list[ControlPoint] = field(default_factory=list)
    wedge: str | None = None
    has_block: bool = False
    has_bolus: bool = False
    fluence_mode: str = "STANDARD"      # STANDARD ou FFF (PrimaryFluenceModeSequence)

    @property
    def n_leaf_pairs(self) -> int | None:
        return None if self.leaf_boundaries is None else len(self.leaf_boundaries) - 1


@dataclass
class Plan:
    sop_instance_uid: str
    patient_id: str
    label: str
    tps: str
    tps_version: str
    n_fractions: int
    prescription: list[dict]
    beams: list[Beam]
    referenced_structure_set: str | None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_mu(self) -> float:
        return sum(b.mu for b in self.beams)


def _bld_lookup(ds_beam) -> dict[str, dict]:
    out = {}
    for d in getattr(ds_beam, "BeamLimitingDeviceSequence", []):
        bnd = getattr(d, "LeafPositionBoundaries", None)
        out[d.RTBeamLimitingDeviceType] = {
            "pairs": int(getattr(d, "NumberOfLeafJawPairs", 1)),
            "boundaries": np.asarray(bnd, float) if bnd is not None else None,
        }
    return out


def _infer_technique(beam: Beam) -> str:
    cps = beam.control_points
    if len(cps) <= 2:
        return "3D"
    gantries = {round(cp.gantry, 2) for cp in cps}
    if len(gantries) > 1:
        return "VMAT"
    # Step & shoot: pares de CPs com mesma abertura e peso crescente
    if beam.beam_type.upper() == "STATIC":
        return "STEP&SHOOT"
    return "DMLC"


def read_rtplan(path: str | Path) -> Plan:
    ds = pydicom.dcmread(str(path), force=True)
    if ds.Modality != "RTPLAN":
        raise ValueError(f"Arquivo não é RTPLAN (Modality={ds.Modality})")
    warnings: list[str] = []

    # MU por feixe a partir do FractionGroup
    beam_mu: dict[int, float] = {}
    n_fx = 0
    for fg in getattr(ds, "FractionGroupSequence", []):
        n_fx = int(getattr(fg, "NumberOfFractionsPlanned", 0) or 0)
        for rb in getattr(fg, "ReferencedBeamSequence", []):
            ms = getattr(rb, "BeamMeterset", None)
            if ms is not None:
                beam_mu[int(rb.ReferencedBeamNumber)] = float(ms)

    prescription = []
    for dr in getattr(ds, "DoseReferenceSequence", []):
        prescription.append({
            "number": int(getattr(dr, "DoseReferenceNumber", 0)),
            "type": str(getattr(dr, "DoseReferenceType", "")),
            "description": str(getattr(dr, "DoseReferenceDescription", "")),
            "target_prescription_dose_gy": (
                float(dr.TargetPrescriptionDose) if "TargetPrescriptionDose" in dr else None),
        })

    beams: list[Beam] = []
    for b in ds.BeamSequence:
        if str(getattr(b, "TreatmentDeliveryType", "TREATMENT")).upper() != "TREATMENT":
            continue  # ignora setup fields
        number = int(b.BeamNumber)
        devices = _bld_lookup(b)
        mlc_axis = next((k for k in devices if k in _MLC), None)
        boundaries = devices[mlc_axis]["boundaries"] if mlc_axis else None
        has_x_jaw = any(k in devices for k in _JAW_X)

        final_w = float(getattr(b, "FinalCumulativeMetersetWeight", 1.0) or 1.0)
        mu = beam_mu.get(number)
        if mu is None:
            warnings.append(f"Feixe {number}: BeamMeterset ausente; MU=0")
            mu = 0.0

        beam = Beam(
            number=number,
            name=str(getattr(b, "BeamName", "")),
            description=str(getattr(b, "BeamDescription", "")),
            machine=str(getattr(b, "TreatmentMachineName", "")),
            radiation_type=str(getattr(b, "RadiationType", "PHOTON")),
            beam_type=str(getattr(b, "BeamType", "STATIC")),
            treatment_delivery=str(getattr(b, "TreatmentDeliveryType", "TREATMENT")),
            technique="",
            mu=mu,
            leaf_boundaries=boundaries,
            sad=float(getattr(b, "SourceAxisDistance", 1000.0)),
            wedge=(str(b.WedgeSequence[0].WedgeID) if int(getattr(b, "NumberOfWedges", 0) or 0) else None),
            has_block=int(getattr(b, "NumberOfBlocks", 0) or 0) > 0,
            has_bolus=int(getattr(b, "NumberOfBoli", 0) or 0) > 0,
        )

        if "PrimaryFluenceModeSequence" in b:
            fm = b.PrimaryFluenceModeSequence[0]
            if str(getattr(fm, "FluenceMode", "")) == "NON_STANDARD" and "FFF" in str(getattr(fm, "FluenceModeID", "")).upper():
                beam.fluence_mode = "FFF"
        prev: ControlPoint | None = None
        for i, c in enumerate(b.ControlPointSequence):
            pos = {d.RTBeamLimitingDeviceType: np.asarray(d.LeafJawPositions, float)
                   for d in getattr(c, "BeamLimitingDevicePositionSequence", [])}

            def jaw(keys, last):
                for k in keys:
                    if k in pos:
                        return (float(pos[k][0]), float(pos[k][1]))
                return last

            if mlc_axis and mlc_axis in pos:
                n = len(pos[mlc_axis]) // 2
                mlc_a, mlc_b = pos[mlc_axis][:n].copy(), pos[mlc_axis][n:].copy()
            else:
                mlc_a = prev.mlc_a if prev else None
                mlc_b = prev.mlc_b if prev else None

            def inh(attr, prev_val, cast=float):
                v = getattr(c, attr, None)
                return cast(v) if v is not None else prev_val

            cp = ControlPoint(
                index=i,
                cumulative_weight=float(c.CumulativeMetersetWeight) / final_w,
                gantry=inh("GantryAngle", prev.gantry if prev else 0.0),
                gantry_dir=inh("GantryRotationDirection", prev.gantry_dir if prev else "NONE", str),
                collimator=inh("BeamLimitingDeviceAngle", prev.collimator if prev else 0.0),
                couch=inh("PatientSupportAngle", prev.couch if prev else 0.0),
                energy_mv=inh("NominalBeamEnergy", prev.energy_mv if prev else None),
                dose_rate=inh("DoseRateSet", prev.dose_rate if prev else None),
                jaw_x=jaw(_JAW_X, prev.jaw_x if prev else None) if has_x_jaw else None,
                jaw_y=jaw(_JAW_Y, prev.jaw_y if prev else None),
                mlc_axis=mlc_axis,
                mlc_a=mlc_a,
                mlc_b=mlc_b,
                isocenter=(tuple(float(x) for x in c.IsocenterPosition)
                           if "IsocenterPosition" in c else (prev.isocenter if prev else (0.0, 0.0, 0.0))),
                ssd=inh("SourceToSurfaceDistance", prev.ssd if prev else None),
            )
            beam.control_points.append(cp)
            prev = cp

        cps = beam.control_points
        for k in range(len(cps) - 1):
            cps[k].mu_segment = (cps[k + 1].cumulative_weight - cps[k].cumulative_weight) * beam.mu
        if cps and abs(cps[-1].cumulative_weight - 1.0) > 1e-4:
            warnings.append(f"Feixe {number}: peso cumulativo final != 1 ({cps[-1].cumulative_weight:.4f})")

        beam.technique = _infer_technique(beam)
        beams.append(beam)

    rss = None
    if "ReferencedStructureSetSequence" in ds:
        rss = str(ds.ReferencedStructureSetSequence[0].ReferencedSOPInstanceUID)

    return Plan(
        sop_instance_uid=str(ds.SOPInstanceUID),
        patient_id=str(getattr(ds, "PatientID", "")),
        label=str(getattr(ds, "RTPlanLabel", "")),
        tps=str(getattr(ds, "ManufacturerModelName", getattr(ds, "Manufacturer", ""))),
        tps_version=str(getattr(ds, "SoftwareVersions", "")),
        n_fractions=n_fx,
        prescription=prescription,
        beams=beams,
        referenced_structure_set=rss,
        warnings=warnings,
    )
