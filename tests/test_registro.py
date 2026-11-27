"""Fusão CT–CT: registro rígido e deformável (demons) em volumes sintéticos. SRS-IMG-040."""
import numpy as np

from tps.imaging import registro as R


def _fantoma(desloc=(0.0, 0.0, 0.0), rot_z=0.0, bexiga=18.0):
    h = 3.0
    x = np.arange(-120, 121, h)
    y = np.arange(-90, 91, h)
    z = np.arange(-60, 61, h)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    c, s = np.cos(np.radians(rot_z)), np.sin(np.radians(rot_z))
    Xr, Yr = c * (X - desloc[0]) + s * (Y - desloc[1]), -s * (X - desloc[0]) + c * (Y - desloc[1])
    Zr = Z - desloc[2]
    hu = np.full(X.shape, -1000.0, np.float32)
    corpo = ((Xr / 110) ** 2 + (Yr / 80) ** 2 <= 1) & (np.abs(Zr) <= 45)
    hu[corpo] = 20.0
    osso = (np.abs(Zr - 10) <= 25) & ((Xr - 40) ** 2 + (Yr - 30) ** 2 <= 14 ** 2) | ((Xr + 40) ** 2 + (Yr - 30) ** 2 <= 14 ** 2)
    hu[osso & corpo] = 900.0
    bex = (Xr ** 2 + (Yr + 15) ** 2 + Zr ** 2) <= bexiga ** 2      # "órgão" que muda de tamanho
    hu[bex] = -60.0
    return hu, R.Grade(x, y, z), bex


def test_rigido_recupera_deslocamento_e_rotacao():
    f, g, _ = _fantoma()
    m, gm, _ = _fantoma(desloc=(7.0, -5.0, 4.0), rot_z=3.0)
    reg = R.rigido(f, g, m, gm)
    assert np.allclose(reg.translacao_mm, (7.0, -5.0, 4.0), atol=1.5)
    assert abs(reg.rotacao_graus[2] - (-3.0)) < 1.0 or abs(reg.rotacao_graus[2] - 3.0) < 1.0
    q0 = R.qualidade(f, g, m, gm, R.Registro())
    q1 = R.qualidade(f, g, m, gm, reg)
    assert q1["ncc"] > q0["ncc"] and q1["mae_hu"] < 0.5 * q0["mae_hu"]


def test_deformavel_corrige_o_orgao_e_propaga():
    f, g, bex_f = _fantoma(bexiga=18.0)
    m, gm, bex_m = _fantoma(desloc=(3.0, 0.0, 0.0), bexiga=27.0)   # bexiga cheia na secundária
    rig = R.rigido(f, g, m, gm)
    q_r = R.qualidade(f, g, m, gm, rig)
    df = R.deformavel(f, g, m, gm, rig)
    q_d = R.qualidade(f, g, m, gm, df)
    assert q_d["mae_hu"] < q_r["mae_hu"] and q_d["ncc"] >= q_r["ncc"]
    assert df.metricas["dobras_pct"] < 0.5 and df.metricas["jacobiano_min"] > 0
    # a bexiga da secundária propagada para a principal fica mais perto da bexiga da principal
    p_r = R.propagar_mascara(bex_m, gm, g, rig)
    p_d = R.propagar_mascara(bex_m, gm, g, df)
    dice = lambda a, b: 2 * (a & b).sum() / (a.sum() + b.sum())   # noqa: E731
    assert dice(p_d, bex_f) > dice(p_r, bex_f) + 0.05
