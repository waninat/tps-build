"""Importação de RTPLAN (ida e volta com a exportação, como no Monaco) e ferramentas de contorno. SRS-IMP/CON."""
import shutil

import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths
from tps.planejamento import contorno as K


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_imp")
    paths.set_home(home)
    paths.ensure_home()
    from tps.app.servidor import app
    caso = home / "fantoma"
    fantoma.gerar_exportacao(caso, calcular_dose=False)
    for f in ("RP.dcm", "RD.dcm"):
        (caso / f).unlink(missing_ok=True)
    c = TestClient(app, raise_server_exceptions=False)
    if c.get("/api/info").json()["configurar"]:
        r = c.post("/api/configurar", json={"login": "fisico.teste", "nome": "Físico Teste", "senha": "senha-forte-1"})
    else:
        r = c.post("/api/login", json={"login": "fisico.teste", "senha": "senha-forte-1"})
    c.headers["X-Token"] = r.json()["token"]
    return c, caso, home


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_exporta_como_monaco_e_reimporta(cli):
    c, caso, home = cli
    s = _ok(c.post("/api/caso/ct", json={"pasta": str(caso)}))
    sid = s["id"]
    _ok(c.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"}))
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "IDAVOLTA", "dose_total": 50, "fracoes": 25,
                                          "alvo": "PTV_fantoma"}))
    for g in (0, 90, 180, 270):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g}))
    _ok(c.post(f"/api/{sid}/calcular", json={"field_in_field": True}))
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1"}))
    ex = _ok(c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1"}))
    rp = pydicom.dcmread(next(a for a in ex["arquivos"] if "RP_" in a))
    # convenções do Monaco 6.1 para o Precise no Mosaiq
    assert rp.SpecificCharacterSet == "ISO_IR 100"
    b = rp.BeamSequence[0]
    assert b.TreatmentMachineName == "Precise" and b.BeamType == "STATIC"
    assert [float(d.SourceToBeamLimitingDeviceDistance) for d in b.BeamLimitingDeviceSequence] == [426.0, 509.0, 377.0]
    assert rp.ToleranceTableSequence[0].ToleranceTableLabel == "1 Fótons" and b.ReferencedToleranceTableNumber == 1
    assert b.InstitutionalDepartmentName == "0~RadioSerra"
    assert all(len(cp.BeamLimitingDevicePositionSequence) == 3 for cp in b.ControlPointSequence)
    rb = rp.FractionGroupSequence[0].ReferencedBeamSequence[0]
    assert float(rb.BeamDose) > 0 and len(rb.BeamDoseSpecificationPoint) == 3
    rs = pydicom.dcmread(next(a for a in ex["arquivos"] if "RS_" in a))
    assert str(rp.ReferencedStructureSetSequence[0].ReferencedSOPInstanceUID) == str(rs.SOPInstanceUID)
    assert {"PTV_fantoma"} <= {r.ROIName for r in rs.StructureSetROISequence}
    # ida e volta: pasta com CT + RS + RP + RD do TPS → importa o próprio plano
    volta = home / "volta"
    volta.mkdir()
    for f in caso.iterdir():
        if f.is_file() and pydicom.dcmread(f, stop_before_pixels=True, force=True).Modality == "CT":
            shutil.copy2(f, volta / f.name)
    for a in ex["arquivos"]:
        if "imagens_" not in a:
            shutil.copy2(a, volta)
    s2 = _ok(c.post("/api/caso/ct", json={"pasta": str(volta)}))
    assert len(s2["planos_dicom"]) == 1 and s2["planos_dicom"][0]["dose"]
    sid2 = s2["id"]
    imp = _ok(c.post(f"/api/{sid2}/importar_plano", json={"arquivo": s2["planos_dicom"][0]["arquivo"]}))
    campos = imp["plano"]["campos"]
    assert imp["plano"]["prescricao"]["metodo"] == "um_fixas"
    assert len(campos) == len([q for q in rp.BeamSequence])
    um_rp = sorted(float(r.BeamMeterset) for r in rp.FractionGroupSequence[0].ReferencedBeamSequence)
    assert sorted(q["mu"] for q in campos) == pytest.approx(um_rp, abs=0.051)
    r = _ok(c.post(f"/api/{sid2}/calcular", json={}))
    cmp = r["resultado"]["comparacao"]
    assert cmp and cmp["gama_3_3"] > 97                       # a mesma dose, por outro caminho (RTDOSE)
    assert abs(cmp["estatisticas"]["PTV_fantoma"]["dif_pct"]) < 1.0
    dv = _ok(c.get(f"/api/{sid2}/dvh"))
    assert "ref" in dv["PTV_fantoma"]


def test_ferramentas_de_contorno_pela_api(cli):
    c, caso, home = cli
    s = _ok(c.post("/api/caso/ct", json={"pasta": str(caso)}))
    sid = s["id"]
    cz = s["ct"]["z"]
    k0 = cz[2] // 2
    _ok(c.post(f"/api/{sid}/contorno/nova", json={"nome": "LESAO"}))
    r = _ok(c.post(f"/api/{sid}/contorno/editar", json={"nome": "LESAO", "idx": k0, "ferramenta": "pincel",
                                                        "pontos": [[0, 0], [10, 0]], "raio_mm": 8}))
    v1 = r["contorno"]["volume_cc"]
    assert v1 > 0
    _ok(c.post(f"/api/{sid}/contorno/editar", json={"nome": "LESAO", "idx": k0 + 6, "ferramenta": "poligono",
                                                    "pontos": [[-10, -10], [10, -10], [10, 10], [-10, 10]]}))
    r = _ok(c.post(f"/api/{sid}/contorno/interpolar", json={"nome": "LESAO"}))
    assert r["contorno"]["novos_cortes"] >= 3
    v2 = next(e["volume_cc"] for e in r["estruturas"] if e["nome"] == "LESAO")
    assert v2 > v1
    r = _ok(c.post(f"/api/{sid}/contorno/editar", json={"nome": "LESAO", "idx": k0, "ferramenta": "pincel",
                                                        "pontos": [[0, 0]], "raio_mm": 30, "apagar": True}))
    _ok(c.post(f"/api/{sid}/contorno/desfazer", json={}))
    sob = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{k0}"))
    assert any(x["nome"] == "LESAO" for x in sob["contornos"])
    r = _ok(c.post(f"/api/{sid}/contorno/corpo", json={"nome": "BODY_AUTO"}))
    assert next(e["volume_cc"] for e in r["estruturas"] if e["nome"] == "BODY_AUTO") > 100
    _ok(c.delete(f"/api/{sid}/estrutura/LESAO"))


def test_contorno_mascara_poligono_ida_e_volta():
    x = np.arange(-50, 50.1, 1.0)
    y = np.arange(-50, 50.1, 1.0)
    m = np.zeros((len(y), len(x)), bool)
    m = K.pincel(m, x, y, [[0, 0]], 20, False)
    m = K.pincel(m, x, y, [[0, 0]], 8, True)               # anel (com buraco)
    polys = K.poligonos_de_mascara(m, x, y, 0.0)
    assert len(polys) == 2
    m2 = K.mascara_de_poligonos(polys, x, y)
    assert (m2 ^ m).sum() / m.sum() < 0.03
    assert not m2[50, 50] and m2[50, 65]
    novos = K.interpolar({0: m, 4: np.zeros_like(m) | K.pincel(np.zeros_like(m), x, y, [[0, 0]], 10, False)},
                         np.arange(5) * 3.0)
    assert set(novos) == {1, 2, 3}
