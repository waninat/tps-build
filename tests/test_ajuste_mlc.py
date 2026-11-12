"""Ajuste de MLC estilo Eclipse, MLCi (não-interdigitação), otimizações 3D e conferência de UM por tabela."""
from pathlib import Path

import numpy as np
import pytest

from tps import fantoma
from tps.engine.beam_model import BeamModelParams
from tps.imaging.hu_ed import HUtoED
from tps.planejamento import calculo
from tps.planejamento.ajuste_mlc import (Margem, OpcoesAjuste, RasterBEV, ajustar, bev_da_estrutura, dilatar,
                                        grade_bev, otimizar_colimador, raster_da_abertura, verificar_mecanica)
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.fluxo import OpcoesPlano3D, planejar_3d
from tps.planejamento.modelo import Campo, Prescricao
from tps.planejamento.otimizacao3d import indice_homogeneidade
from tps.planejamento.um_tabela import TabelasFeixe, dose_por_um_tabela, quadrados_equivalentes

MODELO_TESTE = BeamModelParams(comissionado=True, norm_dose_per_mu=1.0)
RAIZ = Path(__file__).resolve().parents[1]


def _grade(n=61, h=2.0):
    ax = (np.arange(n) - (n - 1) / 2) * h
    Z, Y, X = np.meshgrid(ax, ax, ax, indexing="ij")
    return (ax, ax, ax), X, Y, Z


def test_margem_assimetrica():
    """SRS-PLN-031: X1/X2/Y1/Y2 crescem para o lado certo."""
    u = np.arange(-20, 20.01, 1.0)
    m = np.zeros((len(u), len(u)), bool)
    m[20, 20] = True
    r = dilatar(RasterBEV(u, u.copy(), m), Margem(2, 5, 1, 3))
    iv, iu = np.nonzero(r.m)
    assert (u[iu].min(), u[iu].max(), u[iv].min(), u[iv].max()) == (-2, 5, -1, 3)


@pytest.mark.parametrize("ponto", ["fora", "meio", "area", "dentro"])
def test_ponto_de_encontro_esfera(ponto):
    """SRS-PLN-032: fora ⊇ área ⊇ dentro; 'area' tem área aberta ≈ área do alvo no BEV."""
    eixos, X, Y, Z = _grade()
    esfera = X ** 2 + Y ** 2 + Z ** 2 <= 30 ** 2
    c = Campo("t", 0.0)
    r = ajustar(c, Colimacao.mlci(), [(esfera, Margem())], eixos=eixos, op=OpcoesAjuste(ponto))
    aberta = raster_da_abertura(c, Colimacao.mlci(), r.abertura.u, r.abertura.v)
    alvo = r.abertura.m
    cob = (aberta.m & alvo).sum() / alvo.sum()
    exposto = (aberta.m & ~alvo).sum() / alvo.sum()
    if ponto == "fora":
        assert cob > 0.999
    if ponto == "dentro":
        assert exposto < 0.01          # só o gap mínimo de 5 mm do MLCi nos polos da esfera expõe tecido
    if ponto == "area":
        assert abs(aberta.m.sum() / alvo.sum() - 1) < 0.05
    assert not verificar_mecanica(c, Colimacao.mlci())


def test_mlci_sem_interdigitacao_alvo_obliquo():
    """SRS-PLN-036: alvo em diagonal gera aberturas disjuntas em pares vizinhos; no MLCi o ajuste respeita a
    mecânica (recuando lâminas), no Agility interdigita."""
    eixos, X, Y, Z = _grade(81, 2.0)
    faixa = (np.abs(X - Z) <= 6) & (np.abs(X + Z) <= 70) & (np.abs(Y) <= 10)     # barra estreita a 45°
    for colim, interdig in ((Colimacao.mlci(), False), (Colimacao.agility(), True)):
        c = Campo("t", 0.0)
        r = ajustar(c, colim, [(faixa, Margem.uniforme(2))], eixos=eixos)
        assert verificar_mecanica(c, colim) == []
        a, b = np.array(c.mlc_a), np.array(c.mlc_b)
        viol = sum(a[i] > b[i + 1] - colim.gap_minimo_mm + 1e-6 for i in range(colim.n_pares - 1))
        assert (viol == 0) if not interdig else True
        if not interdig:
            assert r.correcoes_interdigitacao >= 0


def test_mlci_pares_fechados_respeitam_vizinhos():
    eixos, X, Y, Z = _grade()
    cubo = (np.abs(X - 20) <= 15) & (np.abs(Y) <= 15) & (np.abs(Z) <= 15)          # fora do eixo
    c = Campo("t", 0.0)
    ajustar(c, Colimacao.mlci(), [(cubo, Margem.uniforme(5))], eixos=eixos)
    assert verificar_mecanica(c, Colimacao.mlci()) == []
    assert c.jaw_x[0] == pytest.approx(0.0, abs=1.5) and c.jaw_x[1] == pytest.approx(40.7, abs=1.5)


def test_blindagem_de_orgao_corta_a_abertura():
    """SRS-PLN-034: OAR do lado +x é excluído da abertura com a sua margem; alvo tem prioridade."""
    eixos, X, Y, Z = _grade()
    alvo = (np.abs(X) <= 25) & (np.abs(Y) <= 25) & (np.abs(Z) <= 25)
    oar = (X >= 12) & (X <= 40) & (np.abs(Y) <= 25) & (np.abs(Z) <= 45)       # mais longo que o alvo em z
    c0, c1 = Campo("sem", 0.0), Campo("com", 0.0)
    ajustar(c0, Colimacao.agility(), [(alvo, Margem.uniforme(5))], eixos=eixos)
    ajustar(c1, Colimacao.agility(), [(alvo, Margem.uniforme(5))], [(oar, Margem.uniforme(3))], eixos=eixos,
            prioridade_alvo=False)
    assert max(c1.mlc_b) < max(c0.mlc_b) - 15                # banco B recuou para fora do OAR + 3 mm
    # borda do OAR em x = 11 (voxel de 2 mm), projetada da face mais distante: 11·1000/1025 = 10,7; − 3 mm = 7,7
    assert max(c1.mlc_b) == pytest.approx(7.7, abs=0.5)


def test_otimizacao_do_colimador_alinha_com_o_alvo():
    """SRS-PLN-040: retângulo girado 30° no plano do BEV → colimador alinhado (30° ou 120°, a menos do sentido)."""
    eixos, X, Y, Z = _grade(81, 2.0)
    t = np.deg2rad(30)
    xr, zr = X * np.cos(t) + Z * np.sin(t), -X * np.sin(t) + Z * np.cos(t)
    alvo = (np.abs(xr) <= 50) & (np.abs(zr) <= 12) & (np.abs(Y) <= 20)
    ang, custos = otimizar_colimador(Campo("t", 0.0), Colimacao.agility(), [(alvo, Margem.uniforme(3))], eixos,
                                     angulos=range(0, 180, 10))
    resto = ang % 90
    assert min(abs(resto - 30), abs(resto - 60)) <= 10
    assert custos[ang] < custos[0.0]


def test_equivalente_retangular():
    c = Campo("r", 0.0, jaw_x=(-100, 100), jaw_y=(-25, 25), mlc_a=[-100.0] * 40, mlc_b=[100.0] * 40)
    assert quadrados_equivalentes(c, Colimacao.mlci()) == pytest.approx((8.0, 8.0))


# ------------------------------------------------------------------------- com modelo e fantoma
@pytest.fixture
def caso(tmp_path):
    pasta = tmp_path / "fantoma"
    fantoma.gerar_exportacao(pasta, calcular_dose=False)
    return pasta


def test_field_in_field_compensa_separacao_variavel():
    """SRS-PLN-052: AP/PA sobre um corpo cuja separação vai de 12 a 26 cm (pescoço → tórax, sem cunha). O
    field-in-field automático deve homogeneizar o PTV: HI cai ≥ 25%, D98 sobe e o volume acima de 105% cai."""
    from tps.planejamento.otimizacao3d import field_in_field
    from tps.planejamento.modelo import Plano
    m = BeamModelParams.load(RAIZ / "dados/maquinas/oncomed_synergy_154828/6MV/modelo/modelo_6MV.yaml")
    h = 4.0
    x, y, z = np.arange(-150, 151, h), np.arange(-160, 161, h), np.arange(-120, 121, h)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    sep = 120 + (Z + 120) / 240 * 140
    corpo = (np.abs(Y) <= sep / 2) & (np.abs(X) <= 140)
    g = calculo.GradeDose(x, y, z, corpo.astype(np.float32))
    ptv = (np.abs(X) <= 50) & (np.abs(Y) <= 30) & (np.abs(Z) <= 90)
    colim = Colimacao.mlci()
    p = Plano("APPA", "1", "t", "T", Prescricao(40.0, 20, "PTV"))
    for gnt in (0, 180):
        c = Campo(f"G{gnt:03d}", float(gnt))
        ajustar(c, colim, [(ptv, Margem.uniforme(7))], eixos=(x, y, z))
        p.adicionar_campo(c)
    doses = calculo.dose_por_um(g, p.campos, colim, m)
    d0 = calculo.normalizar(p, doses, g, ptv)
    r = field_in_field(p, g, doses, ptv, corpo, colim, m)
    d1 = calculo.normalizar(p, doses, g, ptv)
    assert indice_homogeneidade(d1, ptv) < 0.75 * indice_homogeneidade(d0, ptv)
    assert np.percentile(d1[ptv], 2) > np.percentile(d0[ptv], 2)
    assert ((d1 > 2.1) & corpo).sum() < ((d0 > 2.1) & corpo).sum()
    assert r.subcampos and all(c.mu >= 3.0 for c in r.subcampos)
    for c in p.campos:
        assert verificar_mecanica(c, colim) == []


def test_conferencia_de_um_por_tabela_em_agua():
    """SRS-PLN-027: 10×10 isocêntrico a 10 cm em água — motor (modelo Synergy) × tabelas dentro de 1,5%."""
    pasta = RAIZ / "dados/maquinas/oncomed_synergy_154828/6MV"
    modelo = BeamModelParams.load(pasta / "modelo/modelo_6MV.yaml")
    tab = TabelasFeixe.ler(pasta, 1.0)
    x = np.arange(-150, 151, 3.0)
    y = np.arange(-100, 251, 3.0)
    g = calculo.GradeDose(x, y, x.copy(), np.where(y[None, :, None] >= 0, 1.0, 0.0) * np.ones((len(x), len(y), len(x))))
    c = Campo("A", 0.0, isocentro_mm=(0.0, 100.0, 0.0), jaw_y=(-50, 50), mlc_a=[-50.0] * 80, mlc_b=[50.0] * 80)
    d = calculo.dose_por_um(g, [c], Colimacao.agility(), modelo)["A"]
    motor = calculo.valor_em_ponto(d, g, (0.0, 100.0, 0.0)) * 100
    t = dose_por_um_tabela(c, Colimacao.agility(), tab, (0.0, 100.0, 0.0), g.eta, (g.x, g.y, g.z))
    assert t["prof_rad_mm"] == pytest.approx(100.0, abs=2.0)
    assert motor == pytest.approx(t["cgy_por_um"], rel=0.015)
