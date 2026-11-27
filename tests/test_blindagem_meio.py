"""Blindagem no meio dos pares: o campo é dividido em segmentos step-and-shoot que juntos cobrem a abertura sem a
blindagem, cada ponto aberto em exatamente um segmento (mesma fluência)."""
import numpy as np

from tps.planejamento.ajuste_mlc import (Margem, OpcoesAjuste, ajustar, grade_bev, raster_da_abertura,
                                        verificar_mecanica)
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo


def _caso():
    h = 2.0
    ax = np.arange(-100, 101, h)
    Z, Y, X = np.meshgrid(ax, ax, ax, indexing="ij")
    alvo = (np.abs(X) <= 50) & (np.abs(Y) <= 40) & (np.abs(Z) <= 40)
    oar = (np.abs(X) <= 10) & (Y > 0) & (Y <= 60) & (np.abs(Z) <= 15)      # no meio, atrás (BEV no meio dos pares)
    return (ax, ax, ax), alvo, oar


def _fluencia(c, colim, u, v):
    f = np.zeros((len(v), len(u)))
    for sg in c.segmentos:
        s = Campo(c.nome, c.gantry, jaw_x=sg["jaw_x"] and tuple(sg["jaw_x"]), jaw_y=tuple(sg["jaw_y"]),
                  mlc_a=sg["mlc_a"], mlc_b=sg["mlc_b"])
        f += sg["peso"] * raster_da_abertura(s, colim, u, v).m
    return f


def test_blindagem_no_meio_divide_o_campo():
    eixos, alvo, oar = _caso()
    for colim in (Colimacao.mlci(), Colimacao.agility()):
        c = Campo("G000", 0.0)
        r = ajustar(c, colim, [(alvo, Margem.uniforme(5))], [(oar, Margem.uniforme(0))], eixos,
                    OpcoesAjuste("fora", segmentos="dividir"), prioridade_alvo=False)
        assert c.segmentos_blindagem and len(c.segmentos) >= 2
        assert any("segmentos" in a for a in r.avisos)
        assert sum(s["peso"] for s in c.segmentos) == 1.0
        u, v = grade_bev(200, 1.0)
        f = _fluencia(c, colim, u, v)
        U, V = np.meshgrid(u, v)
        bloq = (np.abs(U) < 7) & (np.abs(V) < 9)          # interior da projeção do órgão (pares inteiros)
        assert f[bloq].max() < 0.05                                 # blindado
        lado = (np.abs(U - 35) < 8) & (np.abs(V) < 10)
        assert np.allclose(f[lado], 1.0 / len(c.segmentos))         # fora: fluência uniforme
        lado2 = (np.abs(U + 35) < 8) & (np.abs(V) < 10)
        assert np.allclose(f[lado2], 1.0 / len(c.segmentos))
        for sg in c.segmentos:
            s = Campo("s", 0.0, jaw_x=sg["jaw_x"] and tuple(sg["jaw_x"]), jaw_y=tuple(sg["jaw_y"]),
                      mlc_a=sg["mlc_a"], mlc_b=sg["mlc_b"])
            assert verificar_mecanica(s, colim) == []
        # a abertura do campo (para FiF/proteção) é a união dos segmentos, não o envelope
        assert not raster_da_abertura(c, colim, u, v).m[bloq].any()


def test_envelope_mantem_aviso():
    eixos, alvo, oar = _caso()
    c = Campo("G000", 0.0)
    r = ajustar(c, Colimacao.agility(), [(alvo, Margem.uniforme(5))], [(oar, Margem.uniforme(0))], eixos,
                OpcoesAjuste("fora"), prioridade_alvo=False)
    assert c.segmentos is None and any("meio do par" in a for a in r.avisos)
