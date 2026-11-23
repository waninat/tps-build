"""Conferência dos atributos obrigatórios (Tipo 1 e 2) do RTPLAN exportado. SRS-EXP-010.

Não substitui um validador completo (ex.: dciodvfy) nem o teste no Mosaiq; serve de rede de segurança automática
em todo teste e em toda exportação.
"""
from __future__ import annotations

from pydicom.dataset import Dataset

# (atributo, tipo) — PS3.3 A.20 (RT Plan IOD), módulos usados pelo TPS
_RAIZ = [("SOPClassUID", 1), ("SOPInstanceUID", 1), ("Modality", 1), ("PatientName", 2), ("PatientID", 2),
         ("PatientBirthDate", 2), ("PatientSex", 2), ("StudyInstanceUID", 1), ("StudyDate", 2), ("StudyTime", 2),
         ("ReferringPhysicianName", 2), ("StudyID", 2), ("AccessionNumber", 2), ("SeriesInstanceUID", 1),
         ("SeriesNumber", 2), ("Manufacturer", 2),
         ("RTPlanLabel", 1), ("RTPlanDate", 2), ("RTPlanTime", 2), ("RTPlanGeometry", 1), ("ApprovalStatus", 1),
         ("FractionGroupSequence", 1), ("BeamSequence", 1), ("PatientSetupSequence", 1)]
_FEIXE = [("BeamNumber", 1), ("BeamType", 1), ("RadiationType", 2), ("TreatmentMachineName", 2),
          ("PrimaryDosimeterUnit", 3), ("SourceAxisDistance", 3), ("BeamLimitingDeviceSequence", 1),
          ("TreatmentDeliveryType", 3), ("NumberOfWedges", 1), ("NumberOfCompensators", 1), ("NumberOfBoli", 1),
          ("NumberOfBlocks", 1), ("FinalCumulativeMetersetWeight", 1), ("NumberOfControlPoints", 1),
          ("ControlPointSequence", 1)]
_CP0 = [("ControlPointIndex", 1), ("CumulativeMetersetWeight", 2), ("NominalBeamEnergy", 3),
        ("BeamLimitingDevicePositionSequence", 1), ("GantryAngle", 1), ("GantryRotationDirection", 1),
        ("BeamLimitingDeviceAngle", 1), ("BeamLimitingDeviceRotationDirection", 1), ("PatientSupportAngle", 1),
        ("PatientSupportRotationDirection", 1), ("TableTopEccentricAngle", 1),
        ("TableTopEccentricRotationDirection", 1), ("TableTopVerticalPosition", 2),
        ("TableTopLongitudinalPosition", 2), ("TableTopLateralPosition", 2), ("IsocenterPosition", 2)]


def _faltas(d: Dataset, regras, onde: str) -> list[str]:
    out = []
    for nome, tipo in regras:
        if tipo == 3:
            continue
        if nome not in d:
            out.append(f"{onde}: falta {nome} (tipo {tipo})")
        elif tipo == 1 and (d[nome].value is None or d[nome].value == "" or
                            (hasattr(d[nome].value, "__len__") and len(d[nome].value) == 0)):
            out.append(f"{onde}: {nome} vazio (tipo 1)")
    return out


def conferir_rtplan(ds: Dataset) -> list[str]:
    erros = _faltas(ds, _RAIZ, "RTPLAN")
    if ds.get("RTPlanGeometry") == "PATIENT":               # módulo Frame of Reference: obrigatório só com imagem
        erros += _faltas(ds, [("FrameOfReferenceUID", 1), ("PositionReferenceIndicator", 2)], "RTPLAN")
    if ds.get("ApprovalStatus") == "APPROVED":
        erros += _faltas(ds, [("ReviewDate", 2), ("ReviewTime", 2), ("ReviewerName", 2)], "RTPLAN")
    if len(str(ds.get("RTPlanLabel", ""))) > 16:
        erros.append("RTPlanLabel com mais de 16 caracteres")
    nums = set()
    for b in ds.get("BeamSequence", []):
        onde = f"feixe {b.get('BeamNumber', '?')}"
        erros += _faltas(b, _FEIXE, onde)
        if str(b.get("TreatmentDeliveryType", "TREATMENT")) != "SETUP":   # setup não entra na fração
            nums.add(int(b.BeamNumber))
        cps = b.get("ControlPointSequence", [])
        if len(cps) != int(b.get("NumberOfControlPoints", -1)):
            erros.append(f"{onde}: NumberOfControlPoints ≠ itens da sequência")
        if cps:
            erros += _faltas(cps[0], _CP0, f"{onde} CP0")
            if float(cps[-1].CumulativeMetersetWeight) != float(b.FinalCumulativeMetersetWeight):
                erros.append(f"{onde}: último CP não fecha o peso final")
        tipos = {d.RTBeamLimitingDeviceType: int(d.NumberOfLeafJawPairs) for d in b.BeamLimitingDeviceSequence}
        for p in cps[0].BeamLimitingDevicePositionSequence if cps else []:
            npar = tipos.get(p.RTBeamLimitingDeviceType)
            if npar is None:
                erros.append(f"{onde}: posição para dispositivo não declarado {p.RTBeamLimitingDeviceType}")
            elif len(p.LeafJawPositions) != 2 * npar:
                erros.append(f"{onde}: {p.RTBeamLimitingDeviceType} com {len(p.LeafJawPositions)} posições")
            else:
                a, bb = p.LeafJawPositions[:npar], p.LeafJawPositions[npar:]
                if any(x > y for x, y in zip(a, bb)):
                    erros.append(f"{onde}: {p.RTBeamLimitingDeviceType} com banco A além do banco B")
    for fg in ds.get("FractionGroupSequence", []):
        refs = {int(r.ReferencedBeamNumber) for r in fg.get("ReferencedBeamSequence", [])}
        if refs != nums:
            erros.append("FractionGroup não referencia exatamente os feixes do plano")
        if int(fg.get("NumberOfBeams", -1)) != len(nums):
            erros.append("NumberOfBeams ≠ número de feixes")
    return erros
