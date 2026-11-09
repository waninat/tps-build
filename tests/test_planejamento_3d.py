"""Fase 1 — conformação, normalização, aprovação e exportação RTPLAN/RTDOSE (ponta a ponta em fantoma)."""
from dataclasses import replace

import numpy as np
import pydicom
import pytest

from tps import fantoma
from tps.dicom_io.rtdose import read_rtdose
from tps.dicom_io.rtplan import read_rtplan
from tps.engine.beam_model import BeamModelParams, ModeloNaoComissionado
from tps.exportacao.rtdose import exportar_rtdose
from tps.exportacao.rtplan import exportar_rtplan
from tps.exportacao.validacao_iod import conferir_rtplan
from tps.imaging.hu_ed import HUtoED
from tps.pipeline import compute_plan_dose
from tps.imaging.ct import read_ct_series
from tps.planejamento import calculo
from tps.planejamento.conformacao import Colimacao, area_aberta_cm2, conformar
from tps.planejamento.fluxo import planejar_3d
from tps.planejamento.modelo import Campo, PlanoAlteradoAposAprovacao, PlanoBloqueado, Prescricao

# Modelo de teste: parâmetros padrão marcados como comissionados SÓ para exercitar o fluxo (não é física validada)
MODELO_TESTE = BeamModelParams(comissionado=True, norm_dose_per_mu=1.0)


def _cubo(n=41, h=2.0, meia=10.0, centro=(0.0, 0.0, 0.0)):
    ax = (np.arange(n) - (n - 1) / 2) * h
    Z, Y, X = np.meshgrid(ax, ax, ax, indexing="ij")
    m = (np.abs(X - centro[0]) <= meia) & (np.abs(Y - centro[1]) <= meia) & (np.abs(Z - centro[2]) <= meia)
    return m, (ax, ax, ax)


# ---------------------------------------------------------------------------------------------- conformação
@pytest.mark.parametrize("gantry", [0.0, 90.0, 180.0, 270.0])
def test_conformacao_cubo_centrado_agility(gantry):
    """Cubo de 20 mm (centros ±10, borda ±11) + margem 5 mm: lâminas em ±16 (com divergência ~1%)."""
    m, eixos = _cubo()
    c = conformar(Campo("t", gantry), m, eixos, Colimacao.agility(), 5.0)
    assert c.jaw_x is None                                    # Agility: sem jaws X
    assert c.jaw_y[0] == pytest.approx(-16.0, abs=0.4) and c.jaw_y[1] == pytest.approx(16.0, abs=0.4)
    a, b = np.array(c.mlc_a), np.array(c.mlc_b)
    abertos = b - a > 1.0
    assert abertos.sum() == 8                                 # faixas de 5 mm de −20 a +20 tocam o alvo + margem
    assert np.allclose(a[abertos], -16.0, atol=0.4) and np.allclose(b[abertos], 16.0, atol=0.4)
    assert np.all(b[~abertos] - a[~abertos] == pytest.approx(0.0))   # gap mínimo do Agility = 0


def test_conformacao_mlci2_gap_minimo_e_jaw_x():
    m, eixos = _cubo()
    c = conformar(Campo("t", 0.0), m, eixos, Colimacao.mlci2(), 5.0)
    assert c.jaw_x is not None and c.jaw_x[0] == pytest.approx(-16.0, abs=0.4)
    gaps = np.array(c.mlc_b) - np.array(c.mlc_a)
    assert gaps.min() >= 5.0 - 1e-9                           # LeafGap 5 mm do MLCi2


def test_conformacao_alvo_fora_do_eixo_segue_o_alvo():
    m, eixos = _cubo(centro=(20.0, 0.0, 0.0))                 # deslocado para a esquerda do paciente (+x)
    c = conformar(Campo("t", 0.0, isocentro_mm=(0.0, 0.0, 0.0)), m, eixos, Colimacao.agility(), 0.0)
    a, b = np.array(c.mlc_a), np.array(c.mlc_b)
    abertos = b - a > 1.0
    # G0, HFS: u = +x DICOM → o alvo aparece em u ≈ +9..+31
    assert np.all(a[abertos] > 5) and np.all(b[abertos] < 35)


def test_area_aberta_quadrado():
    c = Campo("q", 0.0, jaw_y=(-50, 50), mlc_a=[-50.0] * 80, mlc_b=[50.0] * 80)
    assert area_aberta_cm2(c, Colimacao.agility()) == pytest.approx(100.0)


# ---------------------------------------------------------------------------------------------- fluxo completo
@pytest.fixture
def caso_fantoma(tmp_path):
    pasta = tmp_path / "fantoma"
    fantoma.gerar_exportacao(pasta, calcular_dose=False)
    return pasta


def _planejar(pasta, **kw):
    return planejar_3d(pasta, pasta / "RS.dcm", Prescricao(50.0, 25, "PTV_fantoma"), [0, 90, 180, 270],
                       Colimacao.agility(), kw.pop("modelo", MODELO_TESTE), HUtoED.from_csv("siemens_spirit_oncomed"),
                       "teste_agility", "FANTOMA_AGILITY", "CAIXA4", margem_mm=7.0, voxel_mm=4.0, **kw)


def test_modelo_nao_comissionado_recusado(caso_fantoma):
    with pytest.raises(ModeloNaoComissionado):
        _planejar(caso_fantoma, modelo=BeamModelParams())


def test_fluxo_3d_normaliza_exporta_e_releitura_reproduz_a_dose(caso_fantoma, tmp_path):
    r = _planejar(caso_fantoma)
    p = r.plano
    # normalização: média no PTV = 2 Gy/fx (tolerância do arredondamento das UM a 0,1)
    assert r.dose_fracao_gy[r.estruturas["PTV_fantoma"]].mean() == pytest.approx(2.0, rel=2e-3)
    assert all(c.mu > 0 for c in p.campos)
    assert all(c.ssd_mm is not None and 900 < c.ssd_mm < 1000 for c in p.campos)   # cilindro r = 80 mm
    assert r.dvh["PTV_fantoma"]["D95"] > 0.9 * 50.0                                  # caixa 4 campos cobre o PTV

    # exportação exige aprovação
    with pytest.raises(PlanoBloqueado):
        exportar_rtplan(p, Colimacao.agility(), tmp_path / "RP.dcm")
    p.aprovar("fisico.teste", "Físico Médico")
    with pytest.raises(PlanoBloqueado):
        p.adicionar_campo(Campo("extra", 45.0))
    with pytest.raises(PlanoBloqueado):
        p.alterar(rotulo="OUTRO")
    rp = exportar_rtplan(p, Colimacao.agility(), tmp_path / "RP.dcm")
    assert conferir_rtplan(pydicom.dcmread(tmp_path / "RP.dcm")) == []
    rd = exportar_rtdose(r.dose_total_gy, r.grade.x, r.grade.y, r.grade.z, rp, tmp_path / "RD.dcm")

    # releitura pelo leitor do SecDose (independente do escritor)
    lido = read_rtplan(tmp_path / "RP.dcm")
    assert lido.n_fractions == 25 and len(lido.beams) == 4
    for b, c in zip(lido.beams, p.campos):
        assert b.mu == pytest.approx(c.mu) and b.technique == "3D"
        cp = b.control_points[0]
        assert cp.gantry == pytest.approx(c.gantry % 360) and cp.jaw_x is None
        assert np.allclose(cp.mlc_a, c.mlc_a) and np.allclose(cp.mlc_b, c.mlc_b)
        assert np.allclose(cp.jaw_y, c.jaw_y) and np.allclose(cp.isocenter, c.isocentro_mm)

    # a dose recalculada a partir do RTPLAN relido é a mesma do planejamento (prova da exportação)
    ct = read_ct_series(caso_fantoma, "CT*")
    d2 = compute_plan_dose(ct, lido, MODELO_TESTE, HUtoED.from_csv("siemens_spirit_oncomed"),
                           r.grade.x, r.grade.y, r.grade.z) * 25
    assert np.max(np.abs(d2 - r.dose_total_gy)) < 1e-3 * r.dose_total_gy.max()

    dv = read_rtdose(tmp_path / "RD.dcm")
    assert dv.referenced_plan_uid == rp.SOPInstanceUID
    assert np.max(np.abs(dv.dose_gy - r.dose_total_gy)) < 1e-6 * r.dose_total_gy.max() + 1e-9


def test_alteracao_apos_aprovacao_bloqueia_exportacao(caso_fantoma, tmp_path):
    r = _planejar(caso_fantoma)
    r.plano.aprovar("fisico.teste", "Físico Médico")
    r.plano.campos[0].mlc_a[40] -= 5.0          # alteração por fora da API (ex.: bug ou edição indevida)
    with pytest.raises(PlanoAlteradoAposAprovacao):
        exportar_rtplan(r.plano, Colimacao.agility(), tmp_path / "RP.dcm")


def test_desaprovar_exige_motivo_e_registra(caso_fantoma):
    p = _planejar(caso_fantoma).plano
    p.aprovar("fisico.teste", "Físico Médico")
    with pytest.raises(ValueError):
        p.desaprovar("fisico.teste", " ")
    p.desaprovar("fisico.teste", "ajuste de margem")
    assert p.estado == "RASCUNHO" and [h["evento"] for h in p.historico] == ["aprovado", "desaprovado"]


def test_prescricao_invalida_e_rotulo_longo():
    assert Prescricao(0, 1, "PTV").validar()
    assert Prescricao(30, 1, "PTV").validar()                 # 30 Gy em 1 fx > 25 Gy/fx
    assert not Prescricao(60, 30, "PTV").validar()


def test_normalizacao_linear_com_pesos():
    eixos = (np.arange(5.0), np.arange(5.0), np.arange(5.0))
    grade = calculo.GradeDose(*eixos, eta=np.ones((5, 5, 5)))
    from tps.planejamento.modelo import Plano
    p = Plano("P", "X", "m", "M", Prescricao(10.0, 5, "PTV"))
    p.campos = [Campo("A", 0.0, peso=2.0), Campo("B", 90.0, peso=1.0)]
    doses = {"A": np.full((5, 5, 5), 0.01), "B": np.full((5, 5, 5), 0.02)}
    d = calculo.normalizar(p, doses, grade, np.ones((5, 5, 5), bool))
    # 2s·0,01 + s·0,02 = 2 Gy → s = 50 → UM 100 e 50
    assert [c.mu for c in p.campos] == [pytest.approx(100.0), pytest.approx(50.0)]
    assert d.mean() == pytest.approx(2.0)


def test_alvo_maior_que_o_campo_maximo_recusado():
    """SRS-PLN-012: alvo + margem além de 20 cm do eixo é recusado (não gera campo truncado em silêncio)."""
    ax = (np.arange(81) - 40) * 5.0                                  # ±200 mm
    m = np.zeros((81, 81, 81), bool)
    m[:, :, :] = True                                                # alvo ocupa a grade toda
    with pytest.raises(ValueError, match="não cabe"):
        conformar(Campo("t", 0.0), m, (ax, ax, ax), Colimacao.agility(), 5.0)
