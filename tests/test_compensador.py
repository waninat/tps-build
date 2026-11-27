"""Compensador de fluência: homogeneização, mecânica do MLCi, exportação e releitura (SRS-PLN-060..066)."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pydicom
import pytest

from tps.dicom_io.rtplan import read_rtplan
from tps.engine.calc import segments_from_beam
from tps.engine.beam_model import BeamModelParams
from tps.exportacao.rtplan import exportar_rtplan
from tps.exportacao.validacao_iod import conferir_rtplan
from tps.pipeline import compute_segments_dose
from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import Margem, ajustar, verificar_mecanica
from tps.planejamento.compensador import compensar
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo, Plano, Prescricao
from tps.planejamento.otimizacao3d import indice_homogeneidade

RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def caso():
    m = BeamModelParams.load(RAIZ / "dados/maquinas/radioserra_precise/6MV/modelo/modelo_6MV.yaml")
    h = 4.0
    x, y, z = np.arange(-150, 151, h), np.arange(-160, 161, h), np.arange(-120, 121, h)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    sep = 120 + (Z + 120) / 240 * 140                          # separação de 12 a 26 cm
    corpo = (np.abs(Y) <= sep / 2) & (np.abs(X) <= 140)
    g = calculo.GradeDose(x, y, z, corpo.astype(np.float32))
    ptv = (np.abs(X) <= 50) & (np.abs(Y) <= 30) & (np.abs(Z) <= 90)
    colim = Colimacao.mlci()
    p = Plano("COMP", "1", "radioserra_precise", "PRECISE_TESTE", Prescricao(40.0, 20, "PTV"),
              referencia_ct={"PatientName": "TESTE", "PatientID": "1"}, tecnica="2D")
    for gnt in (0, 180):
        c = Campo(f"G{gnt:03d}", float(gnt))
        ajustar(c, colim, [(ptv, Margem.uniforme(7))], eixos=(x, y, z))
        p.adicionar_campo(c)
    p.modelo_feixe = {"energia_mv": 6.0, "hash": calculo.hash_modelo(m), "comissionado": True}
    doses = calculo.dose_por_um(g, p.campos, colim, m)
    d0 = calculo.normalizar(p, doses, g, ptv)
    r = compensar(p, g, doses, ptv, colim, m)
    d1 = calculo.normalizar(p, doses, g, ptv)
    return SimpleNamespace(m=m, g=g, ptv=ptv, colim=colim, plano=p, d0=d0, d1=d1, r=r, corpo=corpo)


def test_compensador_homogeneiza_o_alvo(caso):
    hi0, hi1 = indice_homogeneidade(caso.d0, caso.ptv), indice_homogeneidade(caso.d1, caso.ptv)
    assert hi1 < 0.65 * hi0                                    # melhor que o field-in-field (−15% no mesmo caso)
    assert np.percentile(caso.d1[caso.ptv], 2) > np.percentile(caso.d0[caso.ptv], 2)
    assert caso.r.hi_depois == pytest.approx(hi1, rel=1e-6)


def test_segmentos_respeitam_a_mecanica_e_a_um_minima(caso):
    for c in caso.plano.campos:
        assert c.segmentos and 2 <= len(c.segmentos) <= 16
        assert sum(s["peso"] for s in c.segmentos) == pytest.approx(1.0)
        for s in c.segmentos:
            seg = Campo(c.nome, c.gantry, jaw_x=s["jaw_x"], jaw_y=s["jaw_y"], mlc_a=s["mlc_a"], mlc_b=s["mlc_b"])
            assert verificar_mecanica(seg, caso.colim) == []
            assert s["peso"] * c.mu >= 2.0 - 1e-6


@pytest.mark.parametrize("modo", ["campos_separados", "step_and_shoot"])
def test_exportacao_e_releitura_reproduzem_a_dose(caso, modo, tmp_path):
    p = caso.plano
    if p.estado != "APROVADO":
        p.aprovar("fisico.teste", "Físico Médico")
    exportar_rtplan(p, caso.colim, tmp_path / "RP.dcm", modo_segmentos=modo)
    assert conferir_rtplan(pydicom.dcmread(tmp_path / "RP.dcm")) == []
    lido = read_rtplan(tmp_path / "RP.dcm")
    n_seg = sum(len(c.segmentos) for c in p.campos)
    if modo == "campos_separados":
        assert len(lido.beams) == n_seg and all(b.technique == "3D" for b in lido.beams)
    else:
        assert len(lido.beams) == 2 and all(b.technique == "STEP&SHOOT" for b in lido.beams)
    segs = [s for b in lido.beams for s in segments_from_beam(b)]
    d = compute_segments_dose(SimpleNamespace(patient_position="HFS"), segs, caso.m, None,
                              caso.g.x, caso.g.y, caso.g.z, eta=caso.g.eta)
    # campos separados: UM de cada segmento arredondada a 0,1 → diferença pequena
    tol = 0.01 if modo == "campos_separados" else 1e-3
    assert np.max(np.abs(d - caso.d1)) < tol * caso.d1.max()
