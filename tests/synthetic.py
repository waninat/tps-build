"""Gera RTPLANs sintéticos no padrão Elekta/Monaco para testes."""
from __future__ import annotations

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

RT_PLAN_SOP = "1.2.840.10008.5.1.4.1.1.481.5"


def _bld(dtype, pairs, boundaries=None):
    d = Dataset()
    d.RTBeamLimitingDeviceType = dtype
    d.NumberOfLeafJawPairs = pairs
    if boundaries is not None:
        d.LeafPositionBoundaries = list(boundaries)
    return d


def _pos(dtype, vals):
    d = Dataset()
    d.RTBeamLimitingDeviceType = dtype
    d.LeafJawPositions = [float(v) for v in vals]
    return d


def make_plan(machine="precise", technique="VMAT", mu=300.0, n_cp=5, energy=6) -> Dataset:
    if machine == "precise":
        pairs, width, devices = 40, 10.0, ["ASYMX", "ASYMY", "MLCX"]
    else:  # agility
        pairs, width, devices = 80, 5.0, ["ASYMY", "MLCX"]
    bounds = np.linspace(-pairs * width / 2, pairs * width / 2, pairs + 1)

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = RT_PLAN_SOP
    ds.SOPClassUID = RT_PLAN_SOP
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.Modality = "RTPLAN"
    ds.PatientID = "TESTE001"
    ds.RTPlanLabel = f"T_{technique}"
    ds.Manufacturer = "Elekta"
    ds.ManufacturerModelName = "Monaco"
    ds.SoftwareVersions = "6.x"

    fg = Dataset()
    fg.NumberOfFractionsPlanned = 30
    rb = Dataset()
    rb.ReferencedBeamNumber = 1
    rb.BeamMeterset = mu
    fg.ReferencedBeamSequence = Sequence([rb])
    ds.FractionGroupSequence = Sequence([fg])

    b = Dataset()
    b.BeamNumber = 1
    b.BeamName = "ARC1" if technique == "VMAT" else "G0"
    b.TreatmentMachineName = "LINAC_" + machine.upper()
    b.RadiationType = "PHOTON"
    b.BeamType = "DYNAMIC" if technique == "VMAT" else "STATIC"
    b.TreatmentDeliveryType = "TREATMENT"
    b.SourceAxisDistance = 1000.0
    b.NumberOfWedges = 0
    b.NumberOfBlocks = 0
    b.NumberOfBoli = 0
    b.BeamLimitingDeviceSequence = Sequence(
        [_bld(d, pairs if d.startswith("MLC") else 1, bounds if d.startswith("MLC") else None)
         for d in devices])

    cps = []
    if technique == "3D":
        n_cp = 2
    gantries = np.linspace(180, 179.9 + 360 - 0.1, n_cp) % 360 if technique == "VMAT" else [0.0] * n_cp
    for i in range(n_cp):
        c = Dataset()
        c.ControlPointIndex = i
        c.CumulativeMetersetWeight = i / (n_cp - 1)
        c.GantryAngle = float(gantries[i])
        if i == 0:
            c.NominalBeamEnergy = energy
            c.DoseRateSet = 600
            c.GantryRotationDirection = "CW" if technique == "VMAT" else "NONE"
            c.BeamLimitingDeviceAngle = 0.0
            c.PatientSupportAngle = 0.0
            c.IsocenterPosition = [0.0, 0.0, 0.0]
            c.SourceToSurfaceDistance = 900.0
        if i == 0 or technique == "VMAT":
            half = 20.0 + 5.0 * i
            pos = [_pos("ASYMY", [-50.0, 50.0])]
            if "ASYMX" in devices:
                pos.append(_pos("ASYMX", [-half - 5, half + 5]))
            pos.append(_pos("MLCX", [-half] * pairs + [half] * pairs))
            c.BeamLimitingDevicePositionSequence = Sequence(pos)
        cps.append(c)
    b.ControlPointSequence = Sequence(cps)
    b.NumberOfControlPoints = len(cps)
    b.FinalCumulativeMetersetWeight = 1.0
    ds.BeamSequence = Sequence([b])
    return ds
