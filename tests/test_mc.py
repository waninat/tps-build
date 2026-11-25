"""Monte Carlo: física contra os dados de referência, amostragem, água contra o modelo comissionado, ar. SRS-MC."""
import copy
import math

import numpy as np
import pytest
from numba import njit

from tps.mc import fisica as F
from tps.mc import transporte as T


@pytest.mark.parametrize("e,ref", [(0.01, 22.56), (0.1, 4.115), (1.0, 1.849), (10.0, 1.968)])
def test_poder_de_freamento_da_agua_bate_com_o_estar(e, ref):
    assert float(F.colisao_restrito(e)) == pytest.approx(ref, rel=0.01)


def test_compton_mais_pares_reproduz_a_atenuacao_xcom():
    t = F.tabelas()
    for e in (0.5, 2.0, 6.0, 15.0):
        i = int(round((math.log(e) - t.lne0) / t.dlne))
        soma = t.mu_compton[i] + t.mu_pares[i] + t.mu_resto[i]
        assert soma == pytest.approx(float(F.mu_agua(e)), rel=0.01)


@njit(cache=True)
def _livre_caminho(mu_c, mu_p, mu_r, lne0, dlne, e, n):
    s = 0.0
    for _ in range(n):
        mc = T._tab(mu_c, lne0, dlne, e)
        mp = T._tab(mu_p, lne0, dlne, e)
        mr = T._tab(mu_r, lne0, dlne, e)
        mmax = mc + mp * 1.6 + mr
        d = 0.0
        while True:
            d += -math.log(1 - np.random.random()) / mmax
            if np.random.random() * mmax < mc + mp + mr:
                break
        s += d
    return s / n


def test_woodcock_da_o_livre_caminho_medio_certo():
    t = F.tabelas()
    for e in (0.5, 2.0, 6.0):
        assert _livre_caminho(t.mu_compton, t.mu_pares, t.mu_resto, t.lne0, t.dlne, e, 100000) == \
            pytest.approx(1 / float(F.mu_agua(e)), rel=0.02)


@pytest.fixture(scope="module")
def maquina(tmp_path_factory):
    from tps import paths
    paths.set_home(tmp_path_factory.mktemp("home_mc"))
    paths.ensure_home()
    from tps.app.sessao import Maquina
    return Maquina.carregar("radioserra_precise", "6MV")


def test_pdd_em_agua_acompanha_o_modelo_comissionado(maquina):
    """Só o transporte (sem calibração): a forma do PDD do 10×10 de 5 a 20 cm fica a ±3% do CCC comissionado,
    com o espectro do MC (k ≈ 4,5 g/cm²)."""
    from tps.engine.calc import compute_dose
    from tps.engine.phantom import square_field, water_box
    from tps.mc import calculo as MC
    from tps.planejamento import calculo
    p = maquina.modelo
    q = copy.copy(p)
    q.spectrum_hardening_k = 4.5
    g = water_box(300.0, 260.0, 5.0)
    seg = square_field(10.0, 10.0, 1000.0, 1000.0, 1.0, np.asarray(maquina.colim.limites_laminas))
    a = MC._cax(compute_dose(g, [seg], p, calculo._cones(None, p, False)), 4)
    r = MC.transportar(g, [seg], q, incerteza_pct=0.5, historias_max=6_000_000, historias_min=6_000_000)
    b = MC._cax(r.dose, 4)
    y = g.origin_mm[1] + np.arange(g.eta.shape[1]) * 5.0
    sel = (y >= 50) & (y <= 200)
    razao = (b[sel] / a[sel]) / np.mean(b[sel] / a[sel])
    assert np.all(np.abs(razao - 1) < 0.03)


def test_ar_e_gas_sem_dose_negativa_nem_infinita(maquina):
    from tps.engine.calc import DoseGrid
    from tps.engine.phantom import square_field
    from tps.mc import calculo as MC
    eta = np.ones((40, 40, 40))
    eta[:, :8, :] = 0.0                        # ar na frente
    eta[15:25, 18:22, 15:25] = 0.001           # bolha de gás
    g = DoseGrid(eta, np.array([-97.5, 2.5, -97.5]), np.full(3, 5.0))
    seg = square_field(8.0, 8.0, 1000.0, 1000.0, 1.0, np.asarray(maquina.colim.limites_laminas))
    r = MC.transportar(g, [seg], maquina.modelo, incerteza_pct=5, historias_max=1_000_000, historias_min=1_000_000)
    assert np.isfinite(r.dose).all() and (r.dose >= 0).all()
    # a dose na bolha fica na escala do tecido em volta (sem pico de massa ~0)
    assert r.dose[18:22, 19:21, 18:22].max() < 3 * r.dose[:, 25:30, :].max()


def test_dose_na_agua_no_osso_e_maior_que_no_meio():
    from tps.mc import calculo as MC
    m = MC.materiais(np.array([1.0, 1.7]))
    assert m["agua_por_meio"][0] == pytest.approx(1.0)
    assert 1.08 < m["agua_por_meio"][1] < 1.18                  # (S/ρ)_água/osso ≈ 1,11


def test_hash_do_modelo_e_do_plano_nao_mudam_sem_mc():
    from tps.engine.beam_model import BeamModelParams
    from tps.planejamento import calculo
    from tps.planejamento.modelo import Campo, Plano, Prescricao
    p = BeamModelParams()
    h0 = calculo.hash_modelo(p)
    assert "mc_spectrum_hardening_k" not in str(h0)
    p.mc_spectrum_hardening_k = 4.0
    assert calculo.hash_modelo(p) != h0
    pl = Plano("X", "1", "m", "M", Prescricao(50, 25, "PTV", "media_alvo"),
               [Campo("G0", 0.0, jaw_x=(-50, 50), jaw_y=(-50, 50), mlc_a=[-50.0], mlc_b=[50.0], mu=100.0)])
    assert "calculo_dose" not in pl.conteudo()
