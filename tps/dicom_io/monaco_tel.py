"""Leitor EXPERIMENTAL do formato nativo do Monaco (tel.1). SRS-IO-010.

Objetivo: permitir testes com pastas de paciente do Monaco antes da exportação
DICOM. NÃO é a entrada clínica do sistema — a entrada oficial é DICOM RT.

- Pontos de controle extraídos pelo padrão de expressão regular do pymedphys
  (Apache 2.0), validado pela comunidade com Monaco 5.x.
- Convenções de MLC/colimador seguem o pymedphys e são convertidas para DICOM:
  banco A negativo, banco B positivo, mm no isocentro.
- As MU por CP do tel.1 ainda não foram confirmadas contra o RTPLAN DICOM
  exportado; o plano retornado traz um aviso até essa validação (SRS-IO-011).
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from tps.dicom_io.rtplan import Beam, ControlPoint, Plan

AGILITY_BOUNDARIES = np.linspace(-200.0, 200.0, 81)


def _cp_pattern() -> re.Pattern:
    from pymedphys._monaco.delivery import get_control_point_pattern
    return re.compile(get_control_point_pattern())


def _mlc(block: str) -> tuple[np.ndarray, np.ndarray]:
    raw = np.array(block.replace(" ", "").replace("\n", ",").split(","), float).reshape(80, 2)
    # convenção pymedphys -> (A aberto positivo, B aberto positivo) com ordem de lâminas invertida
    m = raw.copy()
    m[:, 0] = -m[:, 0]
    m = np.fliplr(np.flipud(m))
    a, b = -m[:, 0], m[:, 1]
    # Validado contra o RTPLAN exportado pelo Monaco 6.1.3 (MamaEVMAT, 202 CPs, diferença 0,0 mm):
    # a convenção do pymedphys fica espelhada em relação ao DICOM (bancos e ordem das lâminas).
    return -b[::-1].copy(), -a[::-1].copy()


def read_monaco_tel(tel_path: str | Path, beam_names: list[str] | None = None) -> Plan:
    text = Path(tel_path).read_text(encoding="latin-1").replace("\r", "")
    lines = text.split("\n")
    machine = next((l.strip() for l in lines[15:40] if re.match(r"^[A-Za-z]+\d+[xX]", l.strip())), "")
    energy = re.search(r"(\d+)[xX]$", machine)
    energy_mv = float(energy.group(1)) if energy else None

    matches = list(_cp_pattern().finditer(text))
    if not matches:
        raise ValueError("Nenhum ponto de controle reconhecido no tel.1")

    seg_mu = np.array([float(m.group(5)) for m in matches])
    gantry = np.array([float(m.group(3)) for m in matches])
    coll = np.array([float(m.group(4)) for m in matches])
    jaw_gap = np.array([float(m.group(6)) for m in matches])
    jaw_ctr = np.array([float(m.group(7)) for m in matches])
    static = [m.group(2) == "2,2" for m in matches]

    starts = list(np.where(seg_mu == 0)[0])
    if not starts or starts[0] != 0:
        starts = [0] + starts
    bounds = list(zip(starts, starts[1:] + [len(matches)]))

    beams = []
    for bi, (s, e) in enumerate(bounds):
        mu_beam = float(seg_mu[s:e].sum())
        cum = np.cumsum(seg_mu[s:e]) / mu_beam if mu_beam > 0 else np.zeros(e - s)
        name = beam_names[bi] if beam_names and bi < len(beam_names) else f"Monaco_{bi + 1}"
        beam = Beam(number=bi + 1, name=name, description="monaco_tel", machine=machine,
                    radiation_type="PHOTON", beam_type="STATIC" if any(static[s:e]) else "DYNAMIC",
                    treatment_delivery="TREATMENT", technique="", mu=mu_beam,
                    leaf_boundaries=AGILITY_BOUNDARIES.copy(), sad=1000.0)
        for k in range(s, e):
            a, b = _mlc(matches[k].group(1))
            y2 = jaw_ctr[k] + jaw_gap[k] / 2
            y1 = jaw_ctr[k] - jaw_gap[k] / 2
            cp = ControlPoint(index=k - s, cumulative_weight=float(cum[k - s]),
                              gantry=float(gantry[k]) % 360, gantry_dir="NONE",
                              collimator=float(coll[k]) % 360, couch=0.0, energy_mv=energy_mv,
                              dose_rate=None, jaw_x=None, jaw_y=(float(y1), float(y2)),
                              mlc_axis="MLCX", mlc_a=a, mlc_b=b, isocenter=(np.nan,) * 3)
            beam.control_points.append(cp)
        # MU do segmento i -> i+1 é o incremento registrado no CP i+1
        cps = beam.control_points
        for k in range(len(cps) - 1):
            cps[k].mu_segment = float(seg_mu[s + k + 1])
        if len(cps) > 2:
            d = np.diff([c.gantry for c in cps])
            d = (d + 180) % 360 - 180
            direction = "CW" if np.median(d) > 0 else "CC"
            for c in cps:
                c.gantry_dir = direction
        beam.technique = "VMAT" if len({round(c.gantry, 1) for c in cps}) > 1 else (
            "STEP&SHOOT" if len(cps) > 2 else "3D")
        beams.append(beam)

    return Plan(sop_instance_uid="", patient_id="", label=Path(tel_path).parent.name,
                tps="Monaco (tel.1 nativo)", tps_version="", n_fractions=0, prescription=[],
                beams=beams, referenced_structure_set=None,
                warnings=["Plano lido do formato nativo tel.1 (experimental): MU, isocentro e "
                          "convenção de lâminas a confirmar contra o RTPLAN DICOM exportado."])
