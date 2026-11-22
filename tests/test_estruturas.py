import numpy as np

from tps.planejamento.estruturas import anel, booleana, margem


def test_margem_assimetrica_por_direcao():
    m = np.zeros((41, 41, 41), bool)
    m[20, 20, 20] = True
    r = margem(m, (2.5, 2.5, 2.5), {"direita": 5, "esquerda": 10, "anterior": 0, "posterior": 7.5,
                                    "inferior": 2.5, "superior": 0})
    z, y, x = np.nonzero(r)
    assert (x.min() - 20, x.max() - 20) == (-2, 4)          # direita = −x, esquerda = +x
    assert (y.min() - 20, y.max() - 20) == (0, 3)           # posterior = +y
    assert (z.min() - 20, z.max() - 20) == (-1, 0)          # inferior = −z


def test_contrair_e_anel():
    c = np.zeros((41, 41, 41), bool)
    c[10:31, 10:31, 10:31] = True
    e = margem(c, (2.5, 2.5, 2.5), 5, contrair=True)
    assert e.sum() == 17 ** 3
    a = anel(c, (2.5, 2.5, 2.5), 0, 5)
    assert not (a & c).any() and a.any()
    assert booleana(c, e, "subtracao").sum() == 21 ** 3 - 17 ** 3
