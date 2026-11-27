"""Fusão CT–CT pela API: TC secundária deslocada, registro, imagens da fusão e propagação de estruturas."""
import shutil

import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths

SENHA = "senha-forte-1"


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_fusao")
    paths.set_home(home)
    paths.ensure_home()
    from tps.app.servidor import app
    caso = home / "fantoma"
    fantoma.gerar_exportacao(caso, calcular_dose=False)
    for f in ("RP.dcm", "RD.dcm"):
        (caso / f).unlink(missing_ok=True)
    # secundária: a mesma TC com a paciente 6 mm à esquerda e 4 mm para cima (mesas diferentes), outro referencial
    sec = home / "secundaria"
    sec.mkdir()
    for f in caso.iterdir():
        ds = pydicom.dcmread(str(f), force=True)
        if getattr(ds, "Modality", "") == "CT":
            ds.ImagePositionPatient = [float(ds.ImagePositionPatient[0]) + 6.0, float(ds.ImagePositionPatient[1]) - 4.0,
                                       float(ds.ImagePositionPatient[2])]
            ds.FrameOfReferenceUID = "1.2.3.4.5.6.7.8.9"
            ds.save_as(str(sec / f.name))
        elif getattr(ds, "Modality", "") == "RTSTRUCT":
            for roi in ds.ROIContourSequence:
                for c in getattr(roi, "ContourSequence", []):
                    p = np.asarray(c.ContourData, float).reshape(-1, 3)
                    p[:, 0] += 6.0
                    p[:, 1] -= 4.0
                    c.ContourData = [round(float(v), 3) for v in p.ravel()]
            ds.save_as(str(sec / f.name))
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/configurar", json={"login": "fisico.teste", "nome": "Físico Teste", "senha": SENHA})
    c.headers["X-Token"] = r.json()["token"]
    s = c.post("/api/caso/ct", json={"pasta": str(caso)}).json()
    return c, s["id"], sec


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_fusao_rigida_e_propagacao(cli):
    c, sid, sec = cli
    assert not _ok(c.get(f"/api/{sid}/fusao"))["carregada"]
    assert c.post(f"/api/{sid}/fusao/propagar", json={}).status_code == 400
    e = _ok(c.post(f"/api/{sid}/fusao/carregar", json={"pasta": str(sec)}))
    assert e["carregada"] and not e["mesmo_referencial"] and "PTV_fantoma" in e["estruturas"]
    r = _ok(c.post(f"/api/{sid}/fusao/registrar", json={"modo": "rigido"}))
    t = r["registro"]["translacao_mm"]
    assert abs(t[0] - 6.0) < 1.5 and abs(t[1] + 4.0) < 1.5
    assert r["qualidade"]["depois"]["mae_hu"] < r["qualidade"]["antes"]["mae_hu"]
    for modo in ("mistura", "xadrez", "diferenca", "secundaria"):
        img = c.get(f"/api/{sid}/fusao_corte/axial/10.png?modo={modo}")
        assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    p = _ok(c.post(f"/api/{sid}/fusao/propagar", json={"nomes": ["PTV_fantoma"]}))
    assert p["propagadas"][0]["nome"] == "PTV_fantoma_def"
    ests = {x["nome"]: x for x in p["resumo"]["estruturas"]}
    v0, v1 = ests["PTV_fantoma"]["volume_cc"], ests["PTV_fantoma_def"]["volume_cc"]
    assert abs(v1 - v0) / v0 < 0.1
    # deformável a partir do rígido: não piora e não dobra
    r2 = _ok(c.post(f"/api/{sid}/fusao/registrar", json={"modo": "deformavel", "iteracoes": [20, 10]}))
    assert r2["registro"]["deformavel"] and r2["registro"]["metricas"]["dobras_pct"] < 0.5
    assert r2["qualidade"]["depois"]["mae_hu"] <= r["qualidade"]["depois"]["mae_hu"] * 1.1
    _ok(c.post(f"/api/{sid}/fusao/remover"))
    assert c.get(f"/api/{sid}/fusao_corte/axial/10.png").status_code == 400
