"""Verificações de consistência do plano contra o modelo de máquina. SRS-CHK-001..006.

Estas checagens não calculam dose: validam que o plano é entregável e coerente
antes do cálculo independente.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from tps.dicom_io.rtplan import Beam, Plan
from tps.machine.model import MachineModel, find_machine_for


@dataclass
class Finding:
    level: str        # "ERRO" | "AVISO" | "INFO"
    beam: int | None
    code: str
    message: str


def check_beam(beam: Beam, m: MachineModel) -> list[Finding]:
    f: list[Finding] = []
    mlc = m.mlc

    if beam.n_leaf_pairs is not None and beam.n_leaf_pairs != mlc["leaf_pairs"]:
        f.append(Finding("ERRO", beam.number, "MLC-PAIRS",
                         f"{beam.n_leaf_pairs} pares no plano vs {mlc['leaf_pairs']} no modelo {m.id}"))
    if beam.mu <= 0:
        f.append(Finding("ERRO", beam.number, "MU-ZERO", "Feixe sem MU"))

    energies = [e for e in m.energies if isinstance(e, (int, float))]
    for cp in beam.control_points[:1]:
        if energies and cp.energy_mv is not None and cp.energy_mv not in energies:
            f.append(Finding("ERRO", beam.number, "ENERGY",
                             f"Energia {cp.energy_mv} MV não comissionada em {m.id}"))

    min_gap = mlc.get("min_gap_mm") or 0.0
    overtravel = mlc.get("max_overtravel_mm")
    worst_gap = np.inf
    for cp in beam.control_points:
        if cp.mlc_a is None:
            continue
        gap = cp.mlc_b - cp.mlc_a
        if np.any(gap < -1e-6):
            f.append(Finding("ERRO", beam.number, "MLC-COLLISION",
                             f"CP {cp.index}: lâminas A/B sobrepostas"))
            break
        if cp.jaw_y is not None and beam.leaf_boundaries is not None:
            lb = beam.leaf_boundaries
            inside = (lb[1:] > cp.jaw_y[0]) & (lb[:-1] < cp.jaw_y[1])
            open_gaps = gap[inside]
            if open_gaps.size:
                worst_gap = min(worst_gap, float(open_gaps[open_gaps > 0].min()) if np.any(open_gaps > 0) else np.inf)
        if overtravel is not None:
            if np.any(np.abs(cp.mlc_a) > overtravel + 200) or np.any(np.abs(cp.mlc_b) > overtravel + 200):
                f.append(Finding("AVISO", beam.number, "MLC-RANGE",
                                 f"CP {cp.index}: posição de lâmina fora da faixa esperada"))
                break
        if not m.has_x_jaw and cp.jaw_x is not None:
            f.append(Finding("AVISO", beam.number, "JAW-X",
                             "Plano traz colimador X, mas o modelo não tem"))
            break

    if np.isfinite(worst_gap) and worst_gap < min_gap:
        f.append(Finding("AVISO", beam.number, "MLC-GAP",
                         f"Menor abertura dentro do campo {worst_gap:.1f} mm < {min_gap} mm"))

    # Segmentos com MU muito baixa (step&shoot) são fonte comum de discrepância
    if beam.technique == "STEP&SHOOT":
        seg_mu = [cp.mu_segment for cp in beam.control_points if cp.mu_segment > 1e-6]
        small = [mu for mu in seg_mu if mu < 2.0]
        if small:
            f.append(Finding("INFO", beam.number, "SEG-LOWMU",
                             f"{len(small)} segmento(s) com < 2 MU"))
    return f


def check_plan(plan: Plan, machine: MachineModel | None = None) -> list[Finding]:
    findings = [Finding("AVISO", None, "PARSE", w) for w in plan.warnings]
    for beam in plan.beams:
        m = machine or find_machine_for(beam.machine, beam.n_leaf_pairs)
        if m is None:
            findings.append(Finding("ERRO", beam.number, "MACHINE",
                                    f"Máquina '{beam.machine}' não reconhecida"))
            continue
        findings.extend(check_beam(beam, m))
    return findings
