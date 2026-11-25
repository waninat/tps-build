"""Apagar plano (lixeira), isodoses em dose absoluta, instalação da IA pelo TPS, objetivos por sítio e assistente."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths

SENHA = "senha-forte-1"


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_rodada")
    paths.set_home(home)
    paths.ensure_home()
    from tps.app.servidor import app
    caso = home / "fantoma"
    fantoma.gerar_exportacao(caso, calcular_dose=False)
    for f in ("RP.dcm", "RD.dcm"):
        (caso / f).unlink(missing_ok=True)
    c = TestClient(app, raise_server_exceptions=False)
    if c.get("/api/info").json()["configurar"]:
        r = c.post("/api/configurar", json={"login": "fisico.teste", "nome": "Físico Teste", "senha": SENHA})
    else:
        r = c.post("/api/login", json={"login": "fisico.teste", "senha": SENHA})
    c.headers["X-Token"] = r.json()["token"]
    s = c.post("/api/caso/ct", json={"pasta": str(caso)}).json()
    c.post(f"/api/{s['id']}/maquina", json={"id": "radioserra_precise"})
    return c, s["id"], home


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def _caixa(c, sid, rot):
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": rot, "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    for g in (0, 90, 180, 270):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g}))


def test_apagar_plano_vai_para_a_lixeira(cli):
    c, sid, _ = cli
    home = paths.home()
    _caixa(c, sid, "APAGAR")
    arq = _ok(c.post(f"/api/{sid}/salvar"))["arquivo"]
    assert any(p["rotulo"] == "APAGAR" for p in _ok(c.get("/api/planos")))
    assert c.post(f"/api/{sid}/plano/apagar", json={"rotulo": "APAGAR", "senha": "errada"}).status_code in (400, 401, 403)
    r = _ok(c.post(f"/api/{sid}/plano/apagar", json={"rotulo": "APAGAR", "senha": SENHA}))
    assert not r.get("plano") or r["plano"]["rotulo"] != "APAGAR"
    assert not any(p["rotulo"] == "APAGAR" for p in _ok(c.get("/api/planos")))
    from pathlib import Path
    assert not Path(arq).exists()
    assert list((home / "planos" / "lixeira").glob("*APAGAR*.json"))
    # plano salvo e fechado: apagado pela lista
    _caixa(c, sid, "LISTA")
    _ok(c.post(f"/api/{sid}/salvar"))
    _ok(c.delete(f"/api/{sid}/plano/LISTA"))
    chave = next(p["chave"] for p in _ok(c.get("/api/planos")) if p["rotulo"] == "LISTA")
    _ok(c.post("/api/planos/apagar", json={"chave": chave, "senha": SENHA}))
    assert not any(p["rotulo"] == "LISTA" for p in _ok(c.get("/api/planos")))
    assert len(list((home / "planos" / "lixeira").glob("*.json"))) == 2


def test_isodose_absoluta_junto_com_as_relativas(cli):
    c, sid, _ = cli
    _caixa(c, sid, "ISO")
    _ok(c.post(f"/api/{sid}/calcular", json={}))
    s = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{_meio(c, sid)}", params={"niveis_gy": "40, 25"}))
    absol = [i for i in s["isodoses"] if i.get("absoluta")]
    assert {i["nivel"] for i in absol} == {40.0, 25.0}
    assert all(i["pct"] == pytest.approx(i["nivel"] / s["iso_ref"]["cem_gy"] * 100, abs=0.1) for i in absol)
    assert any(not i.get("absoluta") for i in s["isodoses"]) and s["iso_ref"]["niveis_gy"] == [25.0, 40.0]


def _meio(c, sid):
    return 12                                     # fantoma: 24 cortes


def test_estado_da_instalacao_da_ia(cli):
    c, _, _ = cli
    home = paths.home()
    e = _ok(c.get("/api/ia/instalacao"))
    assert e["rodando"] is False and e["destino"].endswith("ia") and "segmentacao" in e
    assert c.post("/api/ia/apontar", json={"executavel": "/nao/existe", "senha": SENHA}).status_code == 400
    falso = home / "TotalSegmentator"
    falso.write_text("#!/bin/sh\n")
    e = _ok(c.post("/api/ia/apontar", json={"executavel": str(falso), "senha": SENHA}))
    assert e["segmentacao"]["disponivel"] and e["segmentacao"]["executavel"] == str(falso)
    (home / "ia" / "config.yaml").unlink()


def test_objetivos_por_sitio_e_assistente(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "IMRT", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    sitios = {s["id"] for s in _ok(c.get("/api/objetivos_sitio"))}
    assert {"pelve_ginecologica", "prostata", "mama", "cabeca_pescoco", "pulmao"} <= sitios
    r = _ok(c.post(f"/api/{sid}/imrt/assistente", json={"texto": "Próstata, 78 Gy em 39 frações, VMAT"}))
    assert r["sitio"] == "prostata"
    assert r["objetivos"][0] == {"estrutura": "PTV_fantoma", "tipo": "alvo", "dose_gy": 50.0, "volume_pct": None,
                                 "peso": 5.0, "origem": "alvo"}
    assert {f["tg"] for f in r["faltando"]} >= {"Rectum", "Bladder"}      # o fantoma não tem os órgãos
    assert any("prescrição do plano" in a for a in r["avisos"])          # 78 Gy no texto × 50 Gy no plano
    assert r["explicacao"]
    # hipofracionado avisa
    r = _ok(c.post(f"/api/{sid}/imrt/assistente", json={"texto": "mama esquerda 40 Gy em 15 frações"}))
    assert r["sitio"] == "mama" and any("Gy/fração" in a for a in r["avisos"])
    # Sugerir com protocolo e "refazer" não devolvem os objetivos gravados
    obj = _ok(c.get(f"/api/{sid}/imrt/objetivos", params={"sitio": "prostata"}))
    assert obj[0]["tipo"] == "alvo"


def test_assistente_com_orgaos_e_bilaterais(tmp_path):
    from types import SimpleNamespace

    from tps.planejamento import assistente as A
    n = 40
    ax = np.arange(n) * 5.0 - 100
    g = SimpleNamespace(x=ax, y=ax, z=ax)
    corpo = np.zeros((n, n, n), bool)
    corpo[:, 5:35, 5:35] = True
    ptv = np.zeros_like(corpo)
    ptv[15:25, 15:22, 24:30] = True                     # à esquerda (+x)
    pe, pd, cor = (np.zeros_like(corpo) for _ in range(3))
    pe[10:30, 10:30, 21:33] = True
    pd[10:30, 10:30, 7:19] = True
    cor[12:20, 20:28, 14:22] = True
    pe &= ~ptv
    est = {"PTV_6000": ptv, "Pulmao_E": pe, "Pulmao_D": pd, "Coracao": cor, "Body": corpo}
    r = A.analisar(est, g, corpo, 0.125, "pulmão 60 Gy em 30 frações")
    assert r["sitio"] == "pulmao" and r["alvos"] == [{"estrutura": "PTV_6000", "dose_gy": 60.0}]
    assert r["criar"] == [{"nome": "Pulmoes", "operacao": "uniao", "a": "Pulmao_E", "b": "Pulmao_D"}]
    objs = {(o["estrutura"], o["tipo"], o["dose_gy"]) for o in r["objetivos"]}
    assert ("Pulmoes", "dvh", 20.0) in objs and ("Coracao", "media", 18.0) in objs
    assert r["parametros"]["lado"] == "L"
    # sem descrição: o sítio sai da anatomia (órgão mais perto do alvo)
    r = A.analisar(est, g, corpo, 0.125, "")
    assert r["sitio"] in ("pulmao", "mama") and any("anatomia" in e for e in r["explicacao"])
