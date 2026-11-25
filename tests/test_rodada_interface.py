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


class _Resp:
    def __init__(self, blocos):
        from types import SimpleNamespace
        self.content = blocos
        self.usage = SimpleNamespace(input_tokens=1000, output_tokens=200)


class _ClaudeFalso:
    """Simula a API: 1ª volta pede o protocolo e uma medida; 2ª devolve a proposta."""

    def __init__(self):
        self.chamadas = []
        self.messages = self

    def create(self, **kw):
        self.chamadas.append(kw)
        if len(self.chamadas) == 1:
            return _Resp([{"type": "text", "text": "Vou ler o protocolo."},
                          {"type": "tool_use", "id": "t1", "name": "ler_protocolo", "input": {"sitio": "prostata"}},
                          {"type": "tool_use", "id": "t2", "name": "medir", "input": {"a": "Osso_inserto", "b": "PTV_fantoma"}}])
        return _Resp([{"type": "tool_use", "id": "t3", "name": "propor_objetivos", "input": {
            "sitio": "prostata", "explicacao": ["Alvo único com a prescrição"], "avisos": ["Fantoma sem reto"],
            "objetivos": [{"estrutura": "PTV_fantoma", "tipo": "alvo", "dose_gy": 50, "peso": 5, "motivo": "prescrição"},
                          {"estrutura": "Osso_inserto", "tipo": "media", "dose_gy": 20, "peso": 1, "motivo": "teste"},
                          {"estrutura": "Inexistente", "tipo": "max", "dose_gy": 10},
                          {"estrutura": "Osso_inserto", "tipo": "max", "dose_gy": 80}],
            "arcos": {"n": 2, "inicio": 200, "fim": 60, "sentido": "CW"}}}])


def test_assistente_com_claude_simulado(cli, monkeypatch):
    from tps.planejamento import assistente_claude as C
    c, sid, _ = cli
    falso = _ClaudeFalso()
    monkeypatch.setattr(C, "_cliente", lambda chave: falso)
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "CLAUDE", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    assert not _ok(c.get("/api/claude"))["configurado"]
    assert c.post(f"/api/{sid}/imrt/assistente", json={"texto": "x", "motor": "claude"}).status_code == 400
    e = _ok(c.post("/api/claude", json={"chave": "sk-ant-teste-1234", "modelo": "claude-sonnet-5-5", "senha": SENHA}))
    assert e["configurado"] and e["chave_final"] == "1234" and e["modelo"] == "claude-sonnet-5-5"
    assert "sk-ant" not in str(_ok(c.get("/api/claude")))                    # a chave não volta para a tela
    r = _ok(c.post(f"/api/{sid}/imrt/assistente", json={"texto": "FANTOMA próstata 50 Gy", "motor": "auto"}))
    assert r["motor"] == "claude" and r["modelo"] == "claude-sonnet-5-5"
    assert [(o["estrutura"], o["tipo"]) for o in r["objetivos"]] == [("PTV_fantoma", "alvo"), ("Osso_inserto", "media")]
    assert any("Inexistente" in a for a in r["avisos"]) and any("acima da dose do alvo" in a for a in r["avisos"])
    assert r["parametros"]["inicio"] == 200 and r["explicacao"] == ["Alvo único com a prescrição"]
    # o que foi enviado: resumo anônimo, sem o nome do paciente; as ferramentas responderam pelo TPS
    enviado = falso.chamadas[0]["messages"][0]["content"]
    assert "[paciente] próstata" in enviado and "FANTOMA01" not in enviado
    res = falso.chamadas[1]["messages"][2]["content"]
    assert {b["tool_use_id"] for b in res} == {"t1", "t2"} and "distancia_min_mm" in res[1]["content"]
    # aplicar a proposta revisada não chama o Claude de novo
    n = len(falso.chamadas)
    a = _ok(c.post(f"/api/{sid}/imrt/assistente", json={"aplicar": True, "proposta": {"objetivos": r["objetivos"], "criar": []}}))
    assert len(falso.chamadas) == n and len(a["objetivos"]) == 2
    # falha da API: 'auto' cai nas regras com aviso
    def quebra(chave):
        raise RuntimeError("sem internet")
    monkeypatch.setattr(C, "_cliente", quebra)
    r = _ok(c.post(f"/api/{sid}/imrt/assistente", json={"texto": "próstata", "motor": "auto"}))
    assert r["motor"] == "regras" and "Claude indisponível" in r["avisos"][0]
    _ok(c.post("/api/claude", json={"apagar_chave": True, "senha": SENHA}))
    assert not _ok(c.get("/api/claude"))["configurado"]
