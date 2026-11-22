import numpy as np
import pytest

from tps.planejamento import restricoes as R


@pytest.mark.parametrize("nome,tg", [("MEDULA", "SpinalCord"), ("Pulmão D", "Lung_R"), ("pulmao_esq", "Lung_L"),
                                     ("Cabeça Femoral Esq", "Femur_Head_L"), ("Bexiga", "Bladder"), ("RETO", "Rectum"),
                                     ("Coração", "Heart"), ("Lung_L", "Lung_L"), ("PTV", None), ("BODY", "External"),
                                     ("Parotida_D", "Parotid_R"), ("Tronco cerebral", "Brainstem")])
def test_nomes_tg263(nome, tg):
    assert R.nome_tg263(nome) == tg


def test_metricas():
    d = np.linspace(0, 99, 100)            # 100 voxels de 1 cc
    assert R.metrica(d, 1.0, "Dmean", 50) == pytest.approx(49.5)
    assert R.metrica(d, 1.0, "D0.03cc", 50) == pytest.approx(99)
    assert R.metrica(d, 1.0, "D2cc", 50) == pytest.approx(98)
    assert R.metrica(d, 1.0, "V50Gy", 50) == pytest.approx(50)
    assert R.metrica(d, 1.0, "V50Gy_cc", 50) == pytest.approx(50)
    assert R.metrica(d, 1.0, "V95%", 50) == pytest.approx(52)       # ≥ 47,5 Gy: 48…99


def test_avaliar_reto_e_pulmoes_somados():
    rng = np.random.default_rng(0)
    doses = {"PTV": rng.normal(50, 0.5, 2000), "Reto": np.r_[np.full(60, 72.0), np.full(40, 10.0)],
             "Pulmão D": np.full(100, 25.0), "Pulmão E": np.full(100, 5.0)}
    av = R.avaliar(doses, 0.1, 50.0, 25, "PTV", R.carregar(R.PADRAO))
    js = av.json()
    reto = {q["metrica"]: q for q in js["resultados"] if q["tg263"] == "Rectum"}
    assert not reto["V70Gy"]["ok"] and reto["V70Gy"]["valor"] == pytest.approx(60)
    pul = [q for q in js["resultados"] if q["tg263"] == "Lungs"]
    assert pul and {q["metrica"]: q["valor"] for q in pul}["V20Gy"] == pytest.approx(50)
    assert any(q["tg263"] == "alvo" and q["metrica"] == "V95%" and q["ok"] for q in js["resultados"])
    assert js["falhas_limite"] >= 2 and not js["avisos"]


def test_aviso_hipofracionamento():
    av = R.avaliar({"PTV": np.full(10, 40.0)}, 1.0, 40.0, 15, "PTV", R.carregar(R.PADRAO))
    assert av.avisos and "EQD2" in av.avisos[0]
