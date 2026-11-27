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


def test_corpo_3d_pernas_sem_mesa_e_sem_cortes_vazios():
    """BODY: as duas pernas (separadas nos cortes de baixo, ligadas na pelve), sem a mesa, em todos os cortes."""
    import numpy as np
    from tps.planejamento.contorno import corpo_3d, poligonos_de_mascara, sem_aninhados
    nz, ny, nx = 30, 120, 160
    hu = np.full((nz, ny, nx), -1000.0)
    yy, xx = np.mgrid[:ny, :nx]
    for k in range(nz):
        if k >= 15:                                      # pelve: um corpo só
            hu[k][((xx - 80) / 55) ** 2 + ((yy - 55) / 35) ** 2 <= 1] = 30
        else:                                            # pernas: dois círculos separados (a esquerda menor)
            hu[k][(xx - 50) ** 2 + (yy - 60) ** 2 <= 18 ** 2] = 30
            hu[k][(xx - 115) ** 2 + (yy - 60) ** 2 <= 14 ** 2] = 30
        hu[k][100:104, 5:155] = 200                      # mesa (placa fina, maior que a perna em área)
        hu[k][((xx - 80) / 20) ** 2 + ((yy - 55) / 10) ** 2 <= 1] = -900 if k >= 15 else hu[k][55, 80]  # gás
    m = corpo_3d(hu, passo_mm=(3.0, 1.0, 1.0))
    assert m.any(axis=(1, 2)).all()                      # todos os cortes
    assert m[5, 60, 50] and m[5, 60, 115]                # as duas pernas
    assert not m[:, 100:104, :].any()                    # sem a mesa
    assert m[20, 55, 80]                                 # gás dentro do corpo
    x = np.arange(nx, dtype=float)
    y = np.arange(ny, dtype=float)
    p = poligonos_de_mascara(m[5], x, y, 0.0)
    assert len(sem_aninhados(p)) == 2                     # uma linha por perna
    anel = poligonos_de_mascara(m[20], x, y, 0.0) + [np.array([[70, 50, 0], [90, 50, 0], [90, 60, 0], [70, 60, 0]], float)]
    assert len(sem_aninhados(anel)) == 1                 # contorno encaixado dentro de outro sai
