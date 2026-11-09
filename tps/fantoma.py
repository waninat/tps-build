"""Exportação DICOM sintética de um fantoma (CT + RTSTRUCT + RTPLAN + RTDOSE) para testes de instalação.

Serve para conferir, sem dados de paciente, que o caminho completo funciona depois de instalar ou atualizar:
recepção → cálculo independente → comparação → relatório. NÃO valida a física do modelo de feixe: a
"dose do TPS" é a própria dose do SecDose multiplicada por `fator_tps`, então o resultado esperado é
conhecido (diferença ≈ fator_tps − 1 em todo o volume).

Uso típico:
    maquina = instalar_maquina_teste()               # máquina 'fantoma_agility' + modelo 6 MV padrão
    gerar_exportacao(pasta, fator_tps=1.01)          # pasta pronta para importar
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from tps import paths

CT_SOP = "1.2.840.10008.5.1.4.1.1.2"
RTPLAN_SOP = "1.2.840.10008.5.1.4.1.1.481.5"
RTDOSE_SOP = "1.2.840.10008.5.1.4.1.1.481.2"
RTSTRUCT_SOP = "1.2.840.10008.5.1.4.1.1.481.3"
MAQUINA_TESTE = "fantoma_agility"
NOME_DICOM_TESTE = "FANTOMA_AGILITY"


def instalar_maquina_teste(maquina_id: str = MAQUINA_TESTE, nome_dicom: str = NOME_DICOM_TESTE,
                           serial: str = "TESTE-0001") -> str:
    """Cadastra uma máquina Agility de teste com um modelo 6 MV de parâmetros padrão, marcado como de demonstração
    (não comissionado: só calcula fantomas e dados fictícios)."""
    from tps.engine.beam_model import BeamModelParams
    from tps.machine.model import save_machine
    paths.ensure_home()
    save_machine({"id": maquina_id, "fabricante": "Elekta", "modelo": "Fantoma de teste (Agility)", "serial": serial,
                  "nomes_dicom": nome_dicom, "mlc_preset": "agility", "sad_mm": 1000.0, "gap_minimo_mm": 0.0,
                  "energias": ["6MV"], "tecnicas": ["3D", "STEP&SHOOT", "VMAT"], "mlc_transmission": 0.005,
                  "jaw_transmission": 0.005, "interleaf_factor": 1.0, "groove_width_mm": 0.0, "leaf_tip_offset_mm": 0.0})
    paths.models_dir().mkdir(parents=True, exist_ok=True)
    BeamModelParams(demonstracao=True).save(paths.models_dir() / f"{maquina_id}_6MV.yaml")
    return maquina_id


def _base(ds: Dataset, sop_class: str, modality: str, pac: dict, estudo: str, serie: str, frame: str) -> Dataset:
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = sop_class
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.Modality = modality
    ds.PatientID = pac["id"]
    ds.PatientName = pac["nome"]
    ds.PatientBirthDate = pac["nascimento"]
    ds.PatientSex = pac["sexo"]
    ds.StudyInstanceUID = estudo
    ds.SeriesInstanceUID = serie
    ds.FrameOfReferenceUID = frame
    ds.StudyDate = dt.date.today().strftime("%Y%m%d")
    return ds


def _circulo(r: float, z: float, n: int = 48, cx: float = 0.0, cy: float = 0.0) -> list[float]:
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    return [v for a in t for v in (cx + r * np.cos(a), cy + r * np.sin(a), z)]


def _retangulo(hx: float, hy: float, z: float, cx: float = 0.0, cy: float = 0.0) -> list[float]:
    return [v for x, y in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)) for v in (cx + x, cy + y, z)]


def _arco_vmat(rp, fg, maquina_nome_dicom, pairs, bounds, half, mu, n_cp):
    """Um arco de 240° a 120° (horário, passando por 0°) com abertura que varia de forma e posição."""
    rb = Dataset(); rb.ReferencedBeamNumber = 1; rb.BeamMeterset = float(mu)
    fg.ReferencedBeamSequence.append(rb)
    b = Dataset()
    b.BeamNumber, b.BeamName, b.BeamDescription, b.TreatmentMachineName = 1, "Arco1", "A1", maquina_nome_dicom
    b.RadiationType, b.BeamType, b.TreatmentDeliveryType = "PHOTON", "DYNAMIC", "TREATMENT"
    b.SourceAxisDistance = 1000.0
    b.NumberOfWedges = b.NumberOfBlocks = b.NumberOfBoli = 0
    d_y = Dataset(); d_y.RTBeamLimitingDeviceType = "ASYMY"; d_y.NumberOfLeafJawPairs = 1
    d_m = Dataset(); d_m.RTBeamLimitingDeviceType = "MLCX"; d_m.NumberOfLeafJawPairs = pairs
    d_m.LeafPositionBoundaries = [float(v) for v in bounds]
    b.BeamLimitingDeviceSequence = Sequence([d_y, d_m])
    centros = 0.5 * (bounds[:-1] + bounds[1:])
    dentro = np.abs(centros) < half
    cps = []
    for i in range(n_cp):
        f = i / (n_cp - 1)
        g = (240.0 + 240.0 * f) % 360.0
        c = Dataset(); c.ControlPointIndex = i; c.CumulativeMetersetWeight = float(f)
        c.GantryAngle = float(g)
        if i == 0:
            c.NominalBeamEnergy = 6; c.DoseRateSet = 600; c.GantryRotationDirection = "CW"
            c.BeamLimitingDeviceAngle = 0.0; c.PatientSupportAngle = 0.0
            c.IsocenterPosition = [0.0, 0.0, 0.0]; c.SourceToSurfaceDistance = 920.0
        desloc = 15.0 * np.sin(2 * np.pi * f + centros / 30.0)
        largura = half * (0.6 + 0.4 * np.cos(np.pi * f))
        a = np.where(dentro, desloc - largura, -1.0)
        bb = np.where(dentro, desloc + largura, 1.0)
        py = Dataset(); py.RTBeamLimitingDeviceType = "ASYMY"; py.LeafJawPositions = [-half, half]
        pm = Dataset(); pm.RTBeamLimitingDeviceType = "MLCX"
        pm.LeafJawPositions = [round(float(v), 2) for v in np.concatenate([a, bb])]
        c.BeamLimitingDevicePositionSequence = Sequence([py, pm])
        cps.append(c)
    b.ControlPointSequence = Sequence(cps)
    b.NumberOfControlPoints = n_cp
    b.FinalCumulativeMetersetWeight = 1.0
    rp.BeamSequence.append(b)


def gerar_exportacao(pasta: str | Path, maquina_nome_dicom: str = NOME_DICOM_TESTE, fator_tps: float = 1.01,
                     paciente: dict | None = None, campo_cm: float = 8.0, mu: float = 120.0, fracoes: int = 5,
                     prescricao_gy: float = 10.0, rotulo: str = "FANTOMA_3D", calcular_dose: bool = True,
                     curva: str = "siemens_spirit_oncomed", tecnica: str = "3D", n_cp: int = 31) -> dict:
    """Grava CT, RTSTRUCT, RTPLAN e RTDOSE de um cilindro de água em `pasta`: dois campos (G0 e G90) em 3D, ou
    um arco VMAT (240° → 120°, sentido horário) com lâminas que se movem, em tecnica="VMAT"."""
    pasta = Path(pasta)
    pasta.mkdir(parents=True, exist_ok=True)
    pac = {"id": "FANTOMA01", "nome": "FANTOMA^TESTE", "nascimento": "19800101", "sexo": "O", **(paciente or {})}
    estudo, frame = generate_uid(), generate_uid()

    # ------------------------------------------------------------- CT: cilindro de água (r = 80 mm) no ar
    px, nxy, nz, dz = 4.0, 48, 24, 4.0
    xs = (np.arange(nxy) - (nxy - 1) / 2) * px
    zs = (np.arange(nz) - (nz - 1) / 2) * dz
    X, Y = np.meshgrid(xs, xs)
    hu = np.where(X ** 2 + Y ** 2 <= 80.0 ** 2, 0, -1000).astype(np.int16)
    hu[(np.abs(X - 40) < 12) & (np.abs(Y) < 12)] = 700          # inserto de densidade de osso
    serie_ct = generate_uid()
    for k, z in enumerate(zs):
        ds = _base(Dataset(), CT_SOP, "CT", pac, estudo, serie_ct, frame)
        ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
        ds.ImagePositionPatient = [float(xs[0]), float(xs[0]), float(z)]
        ds.PixelSpacing = [px, px]
        ds.SliceThickness = dz
        ds.PatientPosition = "HFS"
        ds.Rows = ds.Columns = nxy
        ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation = 16, 16, 15, 1
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
        ds.RescaleSlope, ds.RescaleIntercept = 1, 0
        ds.Manufacturer, ds.ManufacturerModelName = "SecDose", "Fantoma sintético"
        ds.PixelData = hu.tobytes()
        ds.save_as(pasta / f"CT_{k:03d}.dcm", enforce_file_format=True)

    # ------------------------------------------------------------- RTSTRUCT
    rs = _base(Dataset(), RTSTRUCT_SOP, "RTSTRUCT", pac, estudo, generate_uid(), frame)
    rs.StructureSetLabel = "FANTOMA"
    rois = [(1, "BODY", "EXTERNAL", (0, 255, 0), lambda z: _circulo(80.0, z)),
            (2, "PTV_fantoma", "PTV", (255, 0, 0), lambda z: _retangulo(25.0, 25.0, z) if abs(z) <= 26 else None),
            (3, "Osso_inserto", "ORGAN", (220, 220, 120), lambda z: _retangulo(11.0, 11.0, z, 40.0) if abs(z) <= 30 else None)]
    rs.StructureSetROISequence, rs.ROIContourSequence, rs.RTROIObservationsSequence = Sequence(), Sequence(), Sequence()
    for num, nome, tipo, cor, contorno in rois:
        r = Dataset(); r.ROINumber = num; r.ROIName = nome; r.ReferencedFrameOfReferenceUID = frame
        rs.StructureSetROISequence.append(r)
        c = Dataset(); c.ReferencedROINumber = num; c.ROIDisplayColor = list(cor); c.ContourSequence = Sequence()
        for z in zs:
            pts = contorno(float(z))
            if pts:
                cc = Dataset(); cc.ContourGeometricType = "CLOSED_PLANAR"; cc.NumberOfContourPoints = len(pts) // 3
                cc.ContourData = [round(float(v), 2) for v in pts]
                c.ContourSequence.append(cc)
        rs.ROIContourSequence.append(c)
        o = Dataset(); o.ObservationNumber = num; o.ReferencedROINumber = num; o.RTROIInterpretedType = tipo
        rs.RTROIObservationsSequence.append(o)
    rs.save_as(pasta / "RS.dcm", enforce_file_format=True)

    # ------------------------------------------------------------- RTPLAN: G0 e G90, campo quadrado, Agility
    pairs, width = 80, 5.0
    bounds = np.linspace(-pairs * width / 2, pairs * width / 2, pairs + 1)
    rp = _base(Dataset(), RTPLAN_SOP, "RTPLAN", pac, estudo, generate_uid(), frame)
    rp.RTPlanLabel = rotulo
    rp.Manufacturer, rp.ManufacturerModelName, rp.SoftwareVersions = "SecDose", "Fantoma", "1"
    dr = Dataset(); dr.DoseReferenceNumber = 1; dr.DoseReferenceType = "TARGET"
    dr.DoseReferenceDescription = "PTV_fantoma"; dr.TargetPrescriptionDose = float(prescricao_gy)
    rp.DoseReferenceSequence = Sequence([dr])
    fg = Dataset(); fg.FractionGroupNumber = 1; fg.NumberOfFractionsPlanned = fracoes; fg.ReferencedBeamSequence = Sequence()
    rp.BeamSequence = Sequence()
    half = campo_cm * 5.0
    if tecnica.upper() == "VMAT":
        _arco_vmat(rp, fg, maquina_nome_dicom, pairs, bounds, half, mu, n_cp)
    else:
        for nb, gantry in ((1, 0.0), (2, 90.0)):
            rb = Dataset(); rb.ReferencedBeamNumber = nb; rb.BeamMeterset = float(mu)
            fg.ReferencedBeamSequence.append(rb)
            b = Dataset()
            b.BeamNumber, b.BeamName, b.TreatmentMachineName = nb, f"G{int(gantry)}", maquina_nome_dicom
            b.RadiationType, b.BeamType, b.TreatmentDeliveryType = "PHOTON", "STATIC", "TREATMENT"
            b.SourceAxisDistance = 1000.0
            b.NumberOfWedges = b.NumberOfBlocks = b.NumberOfBoli = 0
            d_y = Dataset(); d_y.RTBeamLimitingDeviceType = "ASYMY"; d_y.NumberOfLeafJawPairs = 1
            d_m = Dataset(); d_m.RTBeamLimitingDeviceType = "MLCX"; d_m.NumberOfLeafJawPairs = pairs
            d_m.LeafPositionBoundaries = [float(v) for v in bounds]
            b.BeamLimitingDeviceSequence = Sequence([d_y, d_m])
            cps = []
            for i in range(2):
                c = Dataset(); c.ControlPointIndex = i; c.CumulativeMetersetWeight = float(i)
                if i == 0:
                    c.NominalBeamEnergy = 6; c.DoseRateSet = 600; c.GantryAngle = gantry
                    c.GantryRotationDirection = "NONE"; c.BeamLimitingDeviceAngle = 0.0; c.PatientSupportAngle = 0.0
                    c.IsocenterPosition = [0.0, 0.0, 0.0]; c.SourceToSurfaceDistance = 920.0
                    py = Dataset(); py.RTBeamLimitingDeviceType = "ASYMY"; py.LeafJawPositions = [-half, half]
                    pm = Dataset(); pm.RTBeamLimitingDeviceType = "MLCX"
                    pm.LeafJawPositions = [-half] * pairs + [half] * pairs
                    c.BeamLimitingDevicePositionSequence = Sequence([py, pm])
                cps.append(c)
            b.ControlPointSequence = Sequence(cps)
            b.NumberOfControlPoints = 2
            b.FinalCumulativeMetersetWeight = 1.0
            rp.BeamSequence.append(b)
    rp.FractionGroupSequence = Sequence([fg])
    rp.save_as(pasta / "RP.dcm", enforce_file_format=True)

    # ------------------------------------------------------------- RTDOSE ("TPS") = dose SecDose × fator
    gx = np.arange(-90.0, 90.1, 4.0)
    gz = zs[2:-2]
    dose = None
    if calcular_dose:
        from tps.dicom_io.rtplan import read_rtplan
        from tps.engine.beam_model import BeamModelParams
        from tps.imaging.ct import read_ct_series
        from tps.imaging.hu_ed import HUtoED
        from tps.machine.model import find_machine_for
        from tps.pipeline import compute_plan_dose
        plan = read_rtplan(pasta / "RP.dcm")
        m = find_machine_for(plan.beams[0].machine, plan.beams[0].n_leaf_pairs)
        if m is None:
            raise ValueError(f"Máquina {maquina_nome_dicom} não cadastrada: rode instalar_maquina_teste() antes.")
        model = BeamModelParams.load(paths.models_dir() / f"{m.id}_6MV.yaml")
        dose = compute_plan_dose(read_ct_series(pasta, "CT_*"), plan, model, HUtoED.from_csv(curva), gx, gx, gz) * fracoes
        dose = dose * fator_tps
    else:
        Zg, Yg, Xg = np.meshgrid(gz, gx, gx, indexing="ij")
        dose = prescricao_gy * np.exp(-(Xg ** 2 + Yg ** 2 + Zg ** 2) / 2500.0)
    rd = _base(Dataset(), RTDOSE_SOP, "RTDOSE", pac, estudo, generate_uid(), frame)
    rd.DoseSummationType, rd.DoseType, rd.DoseUnits = "PLAN", "PHYSICAL", "GY"
    rd.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    rd.ImagePositionPatient = [float(gx[0]), float(gx[0]), float(gz[0])]
    rd.PixelSpacing = [4.0, 4.0]
    rd.GridFrameOffsetVector = [float(v) for v in gz - gz[0]]
    rd.Rows, rd.Columns, rd.NumberOfFrames = len(gx), len(gx), len(gz)
    rd.BitsAllocated, rd.BitsStored, rd.HighBit, rd.PixelRepresentation = 32, 32, 31, 0
    rd.SamplesPerPixel, rd.PhotometricInterpretation = 1, "MONOCHROME2"
    esc = float(dose.max()) / 4e9 if dose.max() > 0 else 1.0
    rd.DoseGridScaling = f"{esc:.10g}"
    rd.PixelData = np.round(dose / esc).astype(np.uint32).tobytes()
    ref = Dataset(); ref.ReferencedSOPClassUID = RTPLAN_SOP; ref.ReferencedSOPInstanceUID = rp.SOPInstanceUID
    rd.ReferencedRTPlanSequence = Sequence([ref])
    rd.save_as(pasta / "RD.dcm", enforce_file_format=True)
    return {"pasta": str(pasta), "sop_plano": str(rp.SOPInstanceUID), "estudo": estudo, "paciente": pac,
            "dose_max_gy": float(dose.max())}
