"""Fluxo automático: TC na pasta → caso + BODY/órgãos → PTV com prescrição no nome + aprovação → plano pronto.
Também a dose em colorwash no BEV. SRS-AUT-001..010."""
import shutil

import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths

SENHA = "senha-forte-1"


def test_prescricao_do_nome():
    from tps.app.automacao import grupos_de_ptv, prescricao_do_nome as P
    assert P("PTVPelve45@25") == {"nome": "PTVPelve45@25", "sitio_txt": "Pelve", "dose_gy": 45.0, "fracoes": 25}
    assert P("PTV_Mama_50@25")["sitio_txt"] == "Mama"
    assert P("PTV 50,4@28")["dose_gy"] == 50.4
    assert P("PTV5040@28")["dose_gy"] == 50.4
    assert P("PTV70@35")["sitio_txt"] == "" and P("PTV70@35")["fracoes"] == 35
    assert P("PTV_45") is None and P("CTV45@25") is None and P("PTV45@99") is None
    g = grupos_de_ptv(["PTV70@35", "PTV56@35", "PTVBoost10@5", "Reto", "BODY"])
    assert [x["rotulo"] for x in g] == ["PTVBoost10@5", "PTV70@35"]
    assert [a["nome"] for a in g[1]["alvos"]] == ["PTV70@35", "PTV56@35"]


@pytest.fixture(scope="module")
def amb(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_auto")
    paths.set_home(home)
    paths.ensure_home()
    from tps.app.servidor import app
    origem = home / "fantoma"
    fantoma.gerar_exportacao(origem, calcular_dose=False)
    entrada = home / "entrada" / "exame1"
    entrada.mkdir(parents=True)
    rs_original = None
    for f in origem.iterdir():
        ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
        if ds.Modality == "CT":
            shutil.copy2(f, entrada / f.name)
        elif ds.Modality == "RTSTRUCT":
            rs_original = f
    c = TestClient(app, raise_server_exceptions=False)
    if c.get("/api/info").json()["configurar"]:
        r = c.post("/api/configurar", json={"login": "fisico.teste", "nome": "Físico Teste", "senha": SENHA})
    else:
        r = c.post("/api/login", json={"login": "fisico.teste", "senha": SENHA})
    c.headers["X-Token"] = r.json()["token"]
    return c, home, entrada, rs_original


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_fluxo_automatico(amb, monkeypatch):
    from tps.app import automacao as AU
    c, home, entrada, rs_original = amb
    monkeypatch.setattr(AU, "iniciar", lambda: None)            # sem threads no teste: as etapas rodam aqui
    cfg = {"ativo": True, "pasta_entrada": str(entrada.parent), "estabilizar_s": 0,
           "plano": {"ativo": True, "gatilho": "aprovacao", "tecnica_por_sitio": {"reto": "3D"},
                     "parecer_claude": False}}
    assert c.post("/api/automacao", json={**cfg, "senha": "errada"}).status_code in (400, 401, 403)
    e = _ok(c.post("/api/automacao", json={**cfg, "senha": SENHA}))
    assert e["config"]["plano"]["tecnica_por_sitio"]["reto"] == "3D"
    # 1. a série de TC chega na pasta
    prontas, rts = AU.varrer(entrada.parent, 0)
    assert len(prontas) == 1 and not rts
    cid = AU.receber_serie(*prontas[0])
    assert AU.receber_serie(*prontas[0]) == cid                 # a mesma série não vira outro caso
    tipo, cid_f = AU._fila.get_nowait()
    assert (tipo, cid_f) == ("contornar", cid)
    # 2. contorno automático (sem IA instalada: BODY)
    d = AU.contornar(cid)
    assert d["estado"].startswith("contornado") and "BODY" in d["estruturas_auto"]
    assert list((AU._pasta_casos() / cid).glob("RS_auto_*.dcm"))
    lista = _ok(c.get("/api/casos"))
    assert lista[0]["id"] == cid
    # 3. o médico desenha o PTV com a prescrição no nome (em outro sistema) e o RTSTRUCT aprovado chega
    ds = pydicom.dcmread(str(rs_original))
    for roi in ds.StructureSetROISequence:
        if roi.ROIName == "PTV_fantoma":
            roi.ROIName = "PTVPelve50@25"
    ds.ApprovalStatus = "APPROVED"
    ds.ReviewerName = "MEDICO^TESTE"
    ds.save_as(str(entrada / "RS_medico.dcm"))
    _, rts = AU.varrer(entrada.parent, 0)
    assert len(rts) == 1
    assert AU.receber_rtstruct(*rts[0]) == cid
    assert AU._fila.get_nowait() == ("planejar", cid)
    # 4. plano automático: nome = nome do PTV, pronto para o físico
    d = AU.planejar(cid)
    assert d["estado"] == "plano pronto — aguardando o físico", d
    p = d["planos"][0]
    assert p["rotulo"] == "PTVPelve50@25" and p["sitio"] == "reto" and p["tecnica"] == "3D"
    assert any(x["rotulo"] == "PTVPelve50@25" for x in _ok(c.get("/api/planos")))
    r = _ok(c.post(f"/api/casos/{cid}/abrir", json={"plano": "PTVPelve50@25"}))
    assert r["plano"]["rotulo"] == "PTVPelve50@25" and r["caso_auto"]["id"] == cid
    sid = r["id"]
    # BEV com dose em colorwash
    _ok(c.post(f"/api/{sid}/calcular", json={}))
    campo = r["plano"]["campos"][0]["nome"]
    for modo in ("max", "iso"):
        img = c.get(f"/api/{sid}/bev_dose/{campo}.png?modo={modo}&minimo=50")
        assert img.status_code == 200 and img.content[:4] == b"\x89PNG"
    from PIL import Image
    import io
    a = np.asarray(Image.open(io.BytesIO(c.get(f"/api/{sid}/bev_dose/{campo}.png?modo=max&minimo=95").content)))
    assert a[..., 3].any() and not a[..., 3].all()             # só onde passa de 95%
    # 5. aprovação das estruturas pelo TPS (caso já existente): grava o RTSTRUCT aprovado
    _ok(c.post("/api/automacao", json={"plano": {"ativo": False}, "senha": SENHA}))
    ap = _ok(c.post(f"/api/{sid}/estruturas/aprovar", json={"senha": SENHA}))
    assert ap["caso"] == cid and not ap["plano_na_fila"] and ap["ptvs"][0]["nome"] == "PTVPelve50@25"
    rs = pydicom.dcmread(ap["arquivo"])
    assert rs.ApprovalStatus == "APPROVED" and rs.ReviewerName == "fisico.teste"
    assert _ok(c.get(f"/api/casos/{cid}"))["estruturas_aprovadas"]["por"] == "fisico.teste"
    _ok(c.post("/api/automacao", json={"ativo": False, "senha": SENHA}))


def test_ct_sem_rtstruct_tem_body(amb):
    c, home, entrada, _ = amb
    pasta = home / "so_ct"
    pasta.mkdir()
    for f in entrada.iterdir():
        if pydicom.dcmread(str(f), stop_before_pixels=True, force=True).Modality == "CT":
            shutil.copy2(f, pasta / f.name)
    r = _ok(c.post("/api/caso/ct", json={"pasta": str(pasta)}))
    assert [e["nome"] for e in r["estruturas"]] == ["BODY"] and r["estruturas"][0]["volume_cc"] > 100
