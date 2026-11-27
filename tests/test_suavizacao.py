"""Pós-processamento de contornos: suavizar preservando o volume, preencher, ilhas. SRS-CNT-020."""
import numpy as np
import pytest

from tps import fantoma, paths
from tps.planejamento import suavizacao as S


def test_suavizar_preserva_volume_e_tira_degraus():
    passo = (3.0, 1.0, 1.0)
    z, y, x = np.mgrid[0:20, 0:80, 0:80]
    m = ((x - 40) ** 2 + (y - 40) ** 2 <= 20 ** 2) & (z > 3) & (z < 16)
    m = np.repeat(np.repeat(m[:, ::4, ::4], 4, axis=1), 4, axis=2)   # degraus de 4 mm (como a IA em grade grossa)
    campo, nivel = S.campo_suave(m, passo, 2.0)
    v = (campo >= nivel).sum()
    assert v == pytest.approx(m.sum(), rel=0.02)
    pol = S.poligonos(campo[10], nivel, np.arange(80.0), np.arange(80.0), 0.0)
    assert len(pol) == 1                                     # um contorno liso, sem ilhas do ruído
    p = pol[0][:, :2]
    c = p.mean(axis=0)
    r = np.hypot(p[:, 0] - c[0], p[:, 1] - c[1])
    assert r.std() < 1.0                                     # quase um círculo


def test_preencher_e_ilhas():
    m = np.zeros((5, 30, 30), bool)
    m[1:4, 5:20, 5:20] = True
    m[2, 10:14, 10:14] = False                               # buraco
    m[2, 25, 25] = True                                      # ilha
    out = S.remover_ilhas(S.preencher(m), 0.01, 0.001)
    assert out[2, 11, 11] and not out[2, 25, 25]


def test_pos_processamento_na_sessao(tmp_path):
    paths.set_home(tmp_path / "h")
    paths.ensure_home()
    caso = tmp_path / "f"
    fantoma.gerar_exportacao(caso, calcular_dose=False)
    from tps.app.sessao import Sessao
    s = Sessao()
    s.carregar_ct(caso)
    cx, cy, cz = s.ct.axes()
    Z, Y, X = np.meshgrid(cz, cy, cx, indexing="ij")
    vol = (np.abs(X) < 30) & (np.abs(Y - 100) < 25) & (np.abs(Z) < 20)
    vol &= ~((np.abs(X) < 5) & (np.abs(Y - 100) < 5))         # buraco no meio
    nome = s._estrutura_de_volume("CAIXA", "ORGAN", (255, 0, 0), vol)
    v0 = s.estruturas[nome].mascara.sum()
    r = s.pos_processar_contorno(nome, suavizar_mm=3, preencher=True, ilhas_cc=0.5)
    assert r["volume_cc"] == pytest.approx(r["volume_antes_cc"], rel=0.15)
    assert s.estruturas[nome].mascara.sum() > 0.9 * v0
    s.desfazer_contorno()
    assert s.estruturas[nome].mascara.sum() == v0
