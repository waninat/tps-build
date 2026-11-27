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
    """[(nome, campo, mu, formas, membros)] — formas: [(jaw_x, jaw_y, mlc_a, mlc_b, peso)]; membros: [(campo, UM)]
    que somam a dose do feixe.

    "step_and_shoot" (como o Monaco): o campo e os seus subcampos de field-in-field viram UM feixe STATIC com pares
    de pontos de controle (peso = UM de cada forma ÷ UM total), e o compensador também. "campos_separados": cada
    subcampo e cada segmento vira um campo estático próprio. Campos de localização vêm à parte (campos_setup)."""
    if modo_segmentos not in ("step_and_shoot", "campos_separados"):
        raise ValueError(f"Modo de segmentos desconhecido: {modo_segmentos}")
    out = []
    principais = [c for c in plano.campos if not c.campo_pai]
    for c in principais:
        filhos = [f for f in plano.campos if f.campo_pai == c.nome]
        if c.pontos_controle:                                  # arco VMAT: um feixe DYNAMIC, sempre
            out.append((c.nome, c, float(c.mu), None, [(c, float(c.mu))]))
            continue
        if modo_segmentos == "step_and_shoot":
            formas, membros, total = [], [(c, float(c.mu))], float(c.mu)
            if c.segmentos:
                formas = [(s.get("jaw_x"), s["jaw_y"], s["mlc_a"], s["mlc_b"], s["peso"] * float(c.mu))
                          for s in c.segmentos]
            else:
                formas = [(c.jaw_x, c.jaw_y, c.mlc_a, c.mlc_b, float(c.mu))]
            for f in filhos:
                formas.append((f.jaw_x, f.jaw_y, f.mlc_a, f.mlc_b, float(f.mu)))
                membros.append((f, float(f.mu)))
                total += float(f.mu)
            formas = [(*fm[:4], fm[4] / total) for fm in formas]
            out.append((c.nome, c, round(total, 1), formas, membros))
        else:
            if not c.segmentos:
                out.append((c.nome, c, float(c.mu), [(c.jaw_x, c.jaw_y, c.mlc_a, c.mlc_b, 1.0)], [(c, float(c.mu))]))
            else:
                for k, s in enumerate(c.segmentos, start=1):
                    mu = round(float(c.mu) * s["peso"], 1)
                    out.append((f"{c.nome[:13]}c{k}", c, mu, [(s.get("jaw_x"), s["jaw_y"], s["mlc_a"], s["mlc_b"], 1.0)],
                                [(c, float(c.mu) * s["peso"])]))
            for f in filhos:
                out.append((f.nome, f, float(f.mu), [(f.jaw_x, f.jaw_y, f.mlc_a, f.mlc_b, 1.0)], [(f, float(f.mu))]))
    return out


def exportar_rtplan(plano: Plano, colim: Colimacao, destino: str | Path, sad_mm: float = 1000.0,
                    exigir_aprovado: bool = True, modo_segmentos: str = "campos_separados",
                    extras: dict | None = None) -> Dataset:
    """Grava o RTPLAN e devolve o Dataset. Por padrão, só exporta plano aprovado e íntegro.

    modo_segmentos (campos com compensador): "campos_separados" (entrega garantida em qualquer R&V) ou
    "step_and_shoot" (um campo STATIC com pares de pontos de controle — como o Monaco grava o field-in-field).
    extras (da máquina e do cálculo, como o Monaco 6.1 grava para o Mosaiq): distancia_fonte_mm {ASYMX, ASYMY, MLCX},
    tolerancia (rótulo), departamento, ponto_ref {"nome", "xyz"} e dose_feixe {nome do feixe: Gy/fração no ponto},
    rtstruct (SOPInstanceUID do RTSTRUCT exportado junto)."""
    ex = extras or {}
    if exigir_aprovado:
        plano.conferir_integridade()
    erros = plano.validar()
    if erros:
        raise ValueError("Plano inválido para exportação: " + "; ".join(erros))
    ref = plano.referencia_ct
    sem_imagem = not ref.get("FrameOfReferenceUID")       # sem CT: geometria do equipamento
    if sem_imagem and plano.tecnica != "2D":
        raise ValueError("Plano sem referência ao CT (FrameOfReferenceUID)")
    agora = dt.datetime.now()

    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = RTPLAN_SOP
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.SOPClassUID = RTPLAN_SOP
    ds.SpecificCharacterSet = "ISO_IR 100"        # Latin-1, como o Monaco (o Mosaiq 2.83 lê assim)
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
    ds.InstanceNumber = 1
    ds.SeriesDate = ds.ContentDate = agora.strftime("%Y%m%d")
    ds.SeriesTime = ds.ContentTime = agora.strftime("%H%M%S")
    if not sem_imagem:
        ds.FrameOfReferenceUID = ref["FrameOfReferenceUID"]
        ds.PositionReferenceIndicator = ""
    ds.Manufacturer = FABRICANTE
    ds.ManufacturerModelName = "TPS"
    ds.SoftwareVersions = __version__
    ds.OperatorsName = (plano.aprovacao or {}).get("usuario", "")

    # ---------------------------------------------------------------- RT General Plan
    conv = ex.get("convencoes") or {}                    # perfil de planejamento (Monaco | Eclipse)
    ds.RTPlanLabel = plano.rotulo[:int(conv.get("rotulo_max", 16))]
    ds.RTPlanName = plano.rotulo
    ds.RTPlanDate, ds.RTPlanTime = agora.strftime("%Y%m%d"), agora.strftime("%H%M%S")
    ds.RTPlanDescription = (f"Planejado no TPS (perfil {conv['sistema']})" if conv.get("sistema") not in (None, "TPS")
                            else "")
    ds.PlanIntent = str(ex.get("intencao") or "CURATIVE")
    ds.RTPlanGeometry = "TREATMENT_DEVICE" if sem_imagem else "PATIENT"
    rs_uid = ex.get("rtstruct") or plano.referencia_rtstruct
    if rs_uid:
        ds.ReferencedStructureSetSequence = Sequence([_ds(ReferencedSOPClassUID=RTSTRUCT_SOP,
                                                          ReferencedSOPInstanceUID=rs_uid)])
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
    refs = [dref]
    pref = ex.get("ponto_ref")
    if pref and not sem_imagem:
        refs.append(_ds(DoseReferenceNumber=2, DoseReferenceUID=generate_uid(),
                        DoseReferenceStructureType="COORDINATES",
                        DoseReferenceDescription=str(pref.get("nome", "REF"))[:64],
                        DoseReferencePointCoordinates=[round(float(v), 1) for v in pref["xyz"]],
                        DoseReferenceType="TARGET"))
    ds.DoseReferenceSequence = Sequence(refs)
    if ex.get("tolerancia"):
        ds.ToleranceTableSequence = Sequence([_ds(ToleranceTableNumber=1, ToleranceTableLabel=str(ex["tolerancia"])[:16])])

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
    setups = list(getattr(plano, "campos_setup", []) or [])
    nomes = [f[0] for f in feixes] + [c.nome for c in setups]
    if len(set(nomes)) != len(nomes):
        raise ValueError("Nomes de feixe repetidos na exportação")
    feixes = feixes + [(c.nome, c, 0.0, [(c.jaw_x, c.jaw_y, c.mlc_a, c.mlc_b, 1.0)], []) for c in setups]

    def posicoes(jx, jy, ma, mb):
        out = []
        if colim.tem_jaw_x:
            out.append(_ds(RTBeamLimitingDeviceType="ASYMX", LeafJawPositions=[float(v) for v in jx]))
        out.append(_ds(RTBeamLimitingDeviceType="ASYMY", LeafJawPositions=[float(v) for v in jy]))
        out.append(_ds(RTBeamLimitingDeviceType=colim.tipo_mlc,
                       LeafJawPositions=[float(v) for v in ma] + [float(v) for v in mb]))
        return Sequence(out)

    for nb, (nome, c, mu, formas, _membros) in enumerate(feixes, start=1):
        e_setup = nb > fg.NumberOfBeams
        if not e_setup:
            if mu < 0.1:
                raise ValueError(f"{nome}: UM abaixo de 0,1")
            rb = _ds(ReferencedBeamNumber=nb, BeamMeterset=round(mu, 1))
            dfx = (ex.get("dose_feixe") or {}).get(nome)
            if pref and not sem_imagem and dfx is not None:
                rb.BeamDoseSpecificationPoint = [round(float(v), 1) for v in pref["xyz"]]
                rb.BeamDose = round(float(dfx), 4)
            fg.ReferencedBeamSequence.append(rb)
        b = Dataset()
        bol = [x for x in plano.bolus if not x.get("campos") or c.nome in x["campos"] or (c.campo_pai or "") in x["campos"]]
        b.BeamNumber, b.BeamName = nb, nome[:16]
        tec = {"IMRT": "IMRT sMLC", "VMAT": "VMAT", "DCA": "Arco conformacional"}.get(plano.tecnica, plano.tecnica)
        desc0 = (getattr(c, "descricao", "") or (tec if conv.get("descricao_feixe") == "tecnica" else nome))
        b.BeamDescription = (desc0
                             + "".join(f" BOLUS {x['espessura_mm']:g}mm" for x in bol))[:64]
        if ex.get("departamento"):
            b.InstitutionalDepartmentName = str(ex["departamento"])[:64]
        b.TreatmentMachineName = plano.nome_dicom_maquina
        b.PrimaryDosimeterUnit = "MU"
        b.SourceAxisDistance = float(sad_mm)
        arco = formas is None
        b.BeamType, b.RadiationType = ("DYNAMIC" if arco else "STATIC"), "PHOTON"
        b.TreatmentDeliveryType = "SETUP" if e_setup else "TREATMENT"
        b.PrimaryFluenceModeSequence = Sequence([_ds(FluenceMode="STANDARD")])
        dist = ex.get("distancia_fonte_mm") or {}

        def disp(tipo, npar):
            d = _ds(RTBeamLimitingDeviceType=tipo)
            if tipo in dist:
                d.SourceToBeamLimitingDeviceDistance = float(dist[tipo])
            d.NumberOfLeafJawPairs = npar
            return d
        dispositivos = []
        if colim.tem_jaw_x:
            dispositivos.append(disp("ASYMX", 1))
        dispositivos.append(disp("ASYMY", 1))
        mlc = disp(colim.tipo_mlc, n)
        mlc.LeafPositionBoundaries = [round(float(v), 2) for v in colim.limites_laminas]
        dispositivos.append(mlc)
        b.BeamLimitingDeviceSequence = Sequence(dispositivos)
        b.ReferencedPatientSetupNumber = 1
        if ex.get("tolerancia"):
            b.ReferencedToleranceTableNumber = 1
        b.NumberOfWedges = b.NumberOfCompensators = b.NumberOfBoli = b.NumberOfBlocks = 0
        b.FinalCumulativeMetersetWeight = 1.0

        cp0 = Dataset()
        cp0.ControlPointIndex = 0
        cp0.NominalBeamEnergy = float(c.energia_mv)
        cp0.DoseRateSet = float(c.taxa_dose)
        pcs = c.pontos_controle if arco else None
        if arco:
            p0 = pcs[0]
            cp0.BeamLimitingDevicePositionSequence = posicoes(p0.get("jaw_x"), p0["jaw_y"], p0["mlc_a"], p0["mlc_b"])
            cp0.GantryAngle = round(float(p0["gantry"]) % 360, 2)
            cp0.GantryRotationDirection = c.sentido
        else:
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
            if not sem_imagem:
                from tps.engine.geometry import beam_frame
                fr = beam_frame(c.gantry, c.colimador, c.mesa, c.isocentro_mm, sad_mm, plano.posicao_paciente)
                cp0.SurfaceEntryPoint = [round(float(v), 1) for v in fr.source + float(c.ssd_mm) * fr.w]
        cp0.CumulativeMetersetWeight = 0.0

        def refdose(w):
            if e_setup:                                   # campo de localização: sem dose
                return Sequence([])
            out = [_ds(ReferencedDoseReferenceNumber=1, CumulativeDoseReferenceCoefficient=round(w, 6))]
            if len(refs) > 1:
                out.append(_ds(ReferencedDoseReferenceNumber=2, CumulativeDoseReferenceCoefficient=round(w, 6)))
            return Sequence(out)
        cp0.ReferencedDoseReferenceSequence = refdose(0.0)
        cps = [cp0]
        acum = 0.0
        if arco:                                         # VMAT: gantry, lâminas e UM acumulada em cada ponto
            for k, pk in enumerate(pcs[1:], start=1):
                cpk = _ds(ControlPointIndex=k)
                cpk.BeamLimitingDevicePositionSequence = posicoes(pk.get("jaw_x"), pk["jaw_y"], pk["mlc_a"], pk["mlc_b"])
                cpk.GantryAngle = round(float(pk["gantry"]) % 360, 2)
                cpk.GantryRotationDirection = c.sentido if k < len(pcs) - 1 else "NONE"
                cpk.CumulativeMetersetWeight = 1.0 if k == len(pcs) - 1 else round(float(pk["mw"]), 6)
                cpk.ReferencedDoseReferenceSequence = refdose(float(cpk.CumulativeMetersetWeight))
                cps.append(cpk)
            formas = []
        for k, (jx, jy, ma, mb, peso) in enumerate(formas):
            if k > 0:                                    # início do segmento k: mesma UM acumulada, nova forma
                cpi = _ds(ControlPointIndex=len(cps), CumulativeMetersetWeight=round(acum, 6))
                cpi.BeamLimitingDevicePositionSequence = posicoes(jx, jy, ma, mb)
                cpi.ReferencedDoseReferenceSequence = refdose(acum)
                cps.append(cpi)
            acum = 1.0 if k == len(formas) - 1 else acum + float(peso)
            cpf = _ds(ControlPointIndex=len(cps))
            # como o Monaco: o fim de cada segmento repete a forma (e o 1º fim repete o gantry)
            cpf.BeamLimitingDevicePositionSequence = posicoes(jx, jy, ma, mb)
            if len(cps) == 1:
                cpf.GantryAngle = cp0.GantryAngle
            cpf.CumulativeMetersetWeight = round(acum, 6)
            cpf.ReferencedDoseReferenceSequence = refdose(acum)
            cps.append(cpf)
        b.NumberOfControlPoints = len(cps)
        b.ControlPointSequence = Sequence(cps)
        ds.BeamSequence.append(b)
    ds.FractionGroupSequence = Sequence([fg])

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(destino, enforce_file_format=True)
    return ds
