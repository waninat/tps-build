"""Exportação de um plano aprovado para DICOM RT Plan (destino: Mosaiq/Elekta). SRS-EXP-001..012.

Segue o RT Plan IOD (DICOM PS3.3 A.20) com as convenções Elekta já validadas na leitura pelo SecDose:
- Agility: dispositivos ASYMY + MLCX (sem colimador X). MLCi/MLCi2: ASYMX + ASYMY + MLCX.
- Campo estático 3D: 2 pontos de controle (peso 0 → 1); o segundo só repete o que o IOD exige.
- UM no FractionGroup (BeamMeterset), com uma casa decimal.

PENDENTE (SRS-EXP-020): conferir contra o DICOM Conformance Statement da versão do Mosaiq da clínica e contra
RTPLANs do Monaco já aceitos pelo Mosaiq (atributos privados, tolerance table, nomes de máquina, SSD, etc.).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Plano
from tps.version import __version__

RTPLAN_SOP = "1.2.840.10008.5.1.4.1.1.481.5"
RTSTRUCT_SOP = "1.2.840.10008.5.1.4.1.1.481.3"
FABRICANTE = "TPS (nome provisorio)"


def referencia_do_ct(arquivo_ct: str | Path) -> dict:
    """Dados de paciente/estudo/referencial lidos de um corte do CT (o plano herda todos)."""
    ds = pydicom.dcmread(str(arquivo_ct), stop_before_pixels=True, force=True)
    return {k: str(getattr(ds, k, "")) for k in ("PatientName", "PatientID", "PatientBirthDate", "PatientSex",
                                                   "StudyInstanceUID", "StudyID", "StudyDate", "StudyTime",
                                                   "AccessionNumber", "ReferringPhysicianName",
                                                   "FrameOfReferenceUID", "SeriesInstanceUID")}


def _ds(**kw) -> Dataset:
    d = Dataset()
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def _feixes_para_exportar(plano: Plano, modo_segmentos: str) -> list[tuple]:
    """[(nome, campo, mu, formas)] — formas: [(jaw_x, jaw_y, mlc_a, mlc_b, peso)].

    Campos com segmentos (compensador): "campos_separados" vira um campo estático por segmento (nome + c1, c2…;
    UM do campo × peso, 0,1 UM); "step_and_shoot" vira um único campo com pares de pontos de controle."""
    out = []
    for c in plano.campos:
        if not c.segmentos:
            out.append((c.nome, c, float(c.mu), [(c.jaw_x, c.jaw_y, c.mlc_a, c.mlc_b, 1.0)]))
        elif modo_segmentos == "step_and_shoot":
            out.append((c.nome, c, float(c.mu), [(s.get("jaw_x"), s["jaw_y"], s["mlc_a"], s["mlc_b"], s["peso"])
                                                 for s in c.segmentos]))
        elif modo_segmentos == "campos_separados":
            for k, s in enumerate(c.segmentos, start=1):
                out.append((f"{c.nome[:13]}c{k}", c, round(float(c.mu) * s["peso"], 1),
                            [(s.get("jaw_x"), s["jaw_y"], s["mlc_a"], s["mlc_b"], 1.0)]))
        else:
            raise ValueError(f"Modo de segmentos desconhecido: {modo_segmentos}")
    return out


def exportar_rtplan(plano: Plano, colim: Colimacao, destino: str | Path, sad_mm: float = 1000.0,
                    exigir_aprovado: bool = True, modo_segmentos: str = "campos_separados") -> Dataset:
    """Grava o RTPLAN e devolve o Dataset. Por padrão, só exporta plano aprovado e íntegro.

    modo_segmentos (campos com compensador): "campos_separados" (padrão, entrega garantida em qualquer R&V) ou
    "step_and_shoot" (um campo com segmentos — PENDENTE de teste no Mosaiq 2.83)."""
    if exigir_aprovado:
        plano.conferir_integridade()
    erros = plano.validar()
    if erros:
        raise ValueError("Plano inválido para exportação: " + "; ".join(erros))
    ref = plano.referencia_ct
    sem_imagem = plano.tecnica == "2D"                     # plano 2D: geometria do equipamento, sem CT
    if not sem_imagem and not ref.get("FrameOfReferenceUID"):
        raise ValueError("Plano sem referência ao CT (FrameOfReferenceUID)")
    agora = dt.datetime.now()

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = RTPLAN_SOP
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = RTPLAN_SOP
    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.InstanceCreationDate, ds.InstanceCreationTime = agora.strftime("%Y%m%d"), agora.strftime("%H%M%S")

    # ---------------------------------------------------------------- paciente / estudo / série / equipamento
    for k in ("PatientName", "PatientID", "PatientBirthDate", "PatientSex", "StudyInstanceUID", "StudyID",
              "StudyDate", "StudyTime", "AccessionNumber", "ReferringPhysicianName"):
        setattr(ds, k, ref.get(k, ""))
    if sem_imagem:
        ds.PatientID = ds.PatientID or plano.paciente_id
        ds.StudyInstanceUID = generate_uid()
        ds.StudyDate, ds.StudyTime = agora.strftime("%Y%m%d"), agora.strftime("%H%M%S")
    ds.Modality = "RTPLAN"
    ds.SeriesInstanceUID = generate_uid()
    ds.SeriesNumber = 1
    if not sem_imagem:
        ds.FrameOfReferenceUID = ref["FrameOfReferenceUID"]
        ds.PositionReferenceIndicator = ""
    ds.Manufacturer = FABRICANTE
    ds.ManufacturerModelName = "TPS"
    ds.SoftwareVersions = __version__
    ds.OperatorsName = (plano.aprovacao or {}).get("usuario", "")

    # ---------------------------------------------------------------- RT General Plan
    ds.RTPlanLabel = plano.rotulo
    ds.RTPlanName = plano.rotulo
    ds.RTPlanDate, ds.RTPlanTime = agora.strftime("%Y%m%d"), agora.strftime("%H%M%S")
    ds.RTPlanGeometry = "TREATMENT_DEVICE" if sem_imagem else "PATIENT"
    if plano.referencia_rtstruct:
        ds.ReferencedStructureSetSequence = Sequence([_ds(ReferencedSOPClassUID=RTSTRUCT_SOP,
                                                          ReferencedSOPInstanceUID=plano.referencia_rtstruct)])
    if plano.aprovacao:
        q = dt.datetime.fromisoformat(plano.aprovacao["quando"])
        ds.ApprovalStatus = "APPROVED"
        ds.ReviewDate, ds.ReviewTime = q.strftime("%Y%m%d"), q.strftime("%H%M%S")
        ds.ReviewerName = plano.aprovacao["usuario"]
    else:
        ds.ApprovalStatus = "UNAPPROVED"

    # ---------------------------------------------------------------- prescrição
    pr = plano.prescricao
    ds.PrescriptionDescription = f"{pr.dose_total_gy:g} Gy / {pr.fracoes} fx"
    dref = _ds(DoseReferenceNumber=1, DoseReferenceUID=generate_uid(), DoseReferenceStructureType="SITE",
               DoseReferenceDescription=pr.estrutura_alvo[:64], DoseReferenceType="TARGET",
               TargetPrescriptionDose=round(pr.dose_total_gy, 4))
    ds.DoseReferenceSequence = Sequence([dref])

    # ---------------------------------------------------------------- setup do paciente
    ds.PatientSetupSequence = Sequence([_ds(PatientSetupNumber=1, PatientPosition=plano.posicao_paciente,
                                            SetupTechnique="ISOCENTRIC")])

    # ---------------------------------------------------------------- fração
    fg = _ds(FractionGroupNumber=1, NumberOfFractionsPlanned=pr.fracoes, NumberOfBeams=0,
             NumberOfBrachyApplicationSetups=0)
    fg.ReferencedDoseReferenceSequence = Sequence([_ds(ReferencedDoseReferenceNumber=1)])
    fg.ReferencedBeamSequence = Sequence()

    # ---------------------------------------------------------------- feixes
    ds.BeamSequence = Sequence()
    n = colim.n_pares
    feixes = _feixes_para_exportar(plano, modo_segmentos)
    fg.NumberOfBeams = len(feixes)

    def posicoes(jx, jy, ma, mb):
        out = []
        if colim.tem_jaw_x:
            out.append(_ds(RTBeamLimitingDeviceType="ASYMX", LeafJawPositions=[float(v) for v in jx]))
        out.append(_ds(RTBeamLimitingDeviceType="ASYMY", LeafJawPositions=[float(v) for v in jy]))
        out.append(_ds(RTBeamLimitingDeviceType=colim.tipo_mlc,
                       LeafJawPositions=[float(v) for v in ma] + [float(v) for v in mb]))
        return Sequence(out)

    for nb, (nome, c, mu, formas) in enumerate(feixes, start=1):
        if mu < 0.1:
            raise ValueError(f"{nome}: UM abaixo de 0,1")
        fg.ReferencedBeamSequence.append(_ds(ReferencedBeamNumber=nb, BeamMeterset=round(mu, 1)))
        b = Dataset()
        b.BeamNumber, b.BeamName, b.BeamDescription = nb, nome[:16], nome
        b.TreatmentMachineName = plano.nome_dicom_maquina
        b.PrimaryDosimeterUnit = "MU"
        b.SourceAxisDistance = float(sad_mm)
        b.BeamType, b.RadiationType, b.TreatmentDeliveryType = "STATIC", "PHOTON", "TREATMENT"
        b.PrimaryFluenceModeSequence = Sequence([_ds(FluenceMode="STANDARD")])
        dispositivos = []
        if colim.tem_jaw_x:
            dispositivos.append(_ds(RTBeamLimitingDeviceType="ASYMX", NumberOfLeafJawPairs=1))
        dispositivos.append(_ds(RTBeamLimitingDeviceType="ASYMY", NumberOfLeafJawPairs=1))
        mlc = _ds(RTBeamLimitingDeviceType=colim.tipo_mlc, NumberOfLeafJawPairs=n)
        mlc.LeafPositionBoundaries = [round(float(v), 2) for v in colim.limites_laminas]
        dispositivos.append(mlc)
        b.BeamLimitingDeviceSequence = Sequence(dispositivos)
        b.ReferencedPatientSetupNumber = 1
        b.NumberOfWedges = b.NumberOfCompensators = b.NumberOfBoli = b.NumberOfBlocks = 0
        b.FinalCumulativeMetersetWeight = 1.0

        cp0 = Dataset()
        cp0.ControlPointIndex = 0
        cp0.NominalBeamEnergy = float(c.energia_mv)
        cp0.DoseRateSet = float(c.taxa_dose)
        cp0.BeamLimitingDevicePositionSequence = posicoes(*formas[0][:4])
        cp0.GantryAngle = float(c.gantry) % 360
        cp0.GantryRotationDirection = "NONE"
        cp0.BeamLimitingDeviceAngle = float(c.colimador) % 360
        cp0.BeamLimitingDeviceRotationDirection = "NONE"
        cp0.PatientSupportAngle = float(c.mesa) % 360
        cp0.PatientSupportRotationDirection = "NONE"
        cp0.TableTopEccentricAngle = 0.0
        cp0.TableTopEccentricRotationDirection = "NONE"
        cp0.TableTopVerticalPosition = cp0.TableTopLongitudinalPosition = cp0.TableTopLateralPosition = None
        cp0.IsocenterPosition = None if sem_imagem else [round(float(v), 2) for v in c.isocentro_mm]
        if c.ssd_mm is not None:
            cp0.SourceToSurfaceDistance = round(float(c.ssd_mm), 1)
        cp0.CumulativeMetersetWeight = 0.0
        cp0.ReferencedDoseReferenceSequence = Sequence([_ds(ReferencedDoseReferenceNumber=1,
                                                            CumulativeDoseReferenceCoefficient=0.0)])
        cps = [cp0]
        acum = 0.0
        for k, (jx, jy, ma, mb, peso) in enumerate(formas):
            if k > 0:                                    # início do segmento k: mesma UM acumulada, nova forma
                cpi = _ds(ControlPointIndex=len(cps), CumulativeMetersetWeight=round(acum, 6))
                cpi.BeamLimitingDevicePositionSequence = posicoes(jx, jy, ma, mb)
                cpi.ReferencedDoseReferenceSequence = Sequence([_ds(ReferencedDoseReferenceNumber=1,
                                                                    CumulativeDoseReferenceCoefficient=round(acum, 6))])
                cps.append(cpi)
            acum = 1.0 if k == len(formas) - 1 else acum + float(peso)
            cpf = _ds(ControlPointIndex=len(cps), CumulativeMetersetWeight=round(acum, 6))
            if len(formas) > 1:
                cpf.BeamLimitingDevicePositionSequence = posicoes(jx, jy, ma, mb)
            cpf.ReferencedDoseReferenceSequence = Sequence([_ds(ReferencedDoseReferenceNumber=1,
                                                                CumulativeDoseReferenceCoefficient=round(acum, 6))])
            cps.append(cpf)
        b.NumberOfControlPoints = len(cps)
        b.ControlPointSequence = Sequence(cps)
        ds.BeamSequence.append(b)
    ds.FractionGroupSequence = Sequence([fg])

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(destino, enforce_file_format=True)
    return ds
