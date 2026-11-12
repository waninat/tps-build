"""Plano 2D sem CT: fantoma virtual, campos retangulares, SAD/SSD, exportação na geometria do equipamento."""
from pathlib import Path

import numpy as np
import pydicom
import pytest

from tps.dicom_io.rtplan import read_rtplan
from tps.engine.beam_model import BeamModelParams
from tps.exportacao.rtplan import exportar_rtplan
from tps.exportacao.validacao_iod import conferir_rtplan
from tps.planejamento import calculo
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Prescricao
from tps.planejamento.plano_2d import CampoRetangular, FantomaVirtual, campo_de_retangulo, planejar_2d
from tps.planejamento.um_tabela import TabelasFeixe

RAIZ = Path(__file__).resolve().parents[1]
MODELO = RAIZ / "dados/maquinas/oncomed_synergy_154828/6MV"


def _modelo():
    return BeamModelParams.load(MODELO / "modelo/modelo_6MV.yaml")


@pytest.mark.parametrize("tipo", ["caixa", "elipse", "contorno"])
def test_fantoma_virtual(tipo):
    f = FantomaVirtual(tipo, 300, 200, 100,
                       contorno_xy=[(-150, -100), (150, -100), (150, 100), (-150, 100)] if tipo == "contorno" else None)
    g = f.grade(5.0)
    meio = g.eta[len(g.z) // 2]
    iy = np.argmin(np.abs(g.y))
    assert meio[iy][np.abs(g.x) <= 140].min() == 1.0 and meio[iy][np.abs(g.x) >= 160].max() == 0.0


def test_campo_retangular_assimetrico_mlci():
    c = campo_de_retangulo(CampoRetangular("AP", 0, 30, 70, 50, 20), Colimacao.mlci(), (0, 0, 0), 6.0)
    assert c.jaw_x == (-30, 70) and c.jaw_y == (-50, 20)
    assert c.mlc_a[20] == -30 and c.mlc_b[20] == 70          # par dentro do campo
    assert c.mlc_b[0] - c.mlc_a[0] == pytest.approx(5.0)      # par sob o jaw: fechado com gap mínimo


def test_ap_pa_sad_coluna_normaliza_no_ponto_e_confere_por_tabela(tmp_path):
    """SRS-2D-003..006: AP/PA 10×10 SAD com ponto no meio de 20 cm de água; UM ≈ cálculo manual; exportação."""
    rx = Prescricao(20.0, 5, "ponto")
    tab = TabelasFeixe.ler(MODELO, 1.0)
    r = planejar_2d(FantomaVirtual("caixa", 300, 200, 300), [CampoRetangular.simetrico("AP", 0, 10, 10),
                    CampoRetangular.simetrico("PA", 180, 10, 10)], rx, Colimacao.agility(), _modelo(),
                    "synergy", "SYNERGY_TESTE", "COLUNA", "123", "TESTE^PACIENTE", tabelas=tab)
    p = r.plano
    assert calculo.valor_em_ponto(r.dose_fracao_gy, r.grade, (0, 0, 0)) == pytest.approx(4.0, rel=2e-3)
    # cálculo manual: 200 cGy por campo / (1,0 × (101,5/100)² × Scp(10) × TMR(10, 10))
    manual = 200.0 / (1.015 ** 2 * tab.tmr_em(10.0, 100.0))
    for c in p.campos:
        assert c.mu == pytest.approx(manual, rel=0.02)
        assert c.ssd_mm == pytest.approx(900.0, abs=1.5)
    assert all(abs(x["dif_pct"]) < 2.0 for x in r.conferencia_um)
    p.aprovar("fisico", "Físico Médico")
    rp = exportar_rtplan(p, Colimacao.agility(), tmp_path / "RP.dcm")
    assert rp.RTPlanGeometry == "TREATMENT_DEVICE" and "FrameOfReferenceUID" not in rp
    assert conferir_rtplan(pydicom.dcmread(tmp_path / "RP.dcm")) == []
    lido = read_rtplan(tmp_path / "RP.dcm")
    assert [b.mu for b in lido.beams] == [c.mu for c in p.campos]


def test_ssd_isocentro_na_pele():
    rx = Prescricao(8.0, 1, "ponto")
    r = planejar_2d(FantomaVirtual("caixa", 300, 200, 300), [CampoRetangular.simetrico("AP", 0, 15, 15)], rx,
                    Colimacao.agility(), _modelo(), "s", "S", "SSD", "1", ponto_prescricao=(0, -70, 0), tecnica="SSD")
    c = r.plano.campos[0]
    assert c.isocentro_mm[1] == pytest.approx(-100.0, abs=1.0)    # pele anterior
    assert c.ssd_mm == pytest.approx(1000.0, abs=1.5)
