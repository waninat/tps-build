"""Copiloto do plano com o Claude (API simulada): lê, age no rascunho, dá parecer, desfaz. SRS-PLN-130."""
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from tps import fantoma, paths

SENHA = "senha-forte-1"


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_copiloto")
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


class _Resp:
    def __init__(self, blocos):
        self.content = blocos
        self.usage = SimpleNamespace(input_tokens=500, output_tokens=100)


class _Falso:
    def __init__(self, roteiro):
        self.roteiro = list(roteiro)
        self.chamadas = []
        self.messages = self

    def create(self, **kw):
        self.chamadas.append(kw)
        return _Resp(self.roteiro.pop(0))


def _tu(i, nome, entrada):
    return {"type": "tool_use", "id": i, "name": nome, "input": entrada}


def test_copiloto_le_age_e_desfaz(cli, monkeypatch):
    from tps.planejamento import assistente_claude as C
    c, sid, home = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "COPI", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    for g in (0, 90, 180, 270):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g}))
    _ok(c.post(f"/api/{sid}/calcular", json={}))
    assert c.post(f"/api/{sid}/copiloto", json={"texto": "avalie"}).status_code == 400     # sem chave
    _ok(c.post("/api/claude", json={"chave": "sk-ant-teste-9999", "senha": SENHA}))
    falso = _Falso([
        [{"type": "text", "text": "Vou ler o plano."}, _tu("a", "ler_plano", {}), _tu("b", "ler_dose", {})],
        [_tu("c", "bloquear_orgao", {"campo": "G090", "estrutura": "Osso_inserto", "margem_mm": 2}),
         _tu("d", "aprovar", {})],
        [_tu("e", "calcular", {"otimizar_pesos": True})],
        [_tu("f", "dar_parecer", {"aval": "melhorar", "resumo": "Cobertura boa; osso protegido no lateral.",
                                  "pontos": ["HI ok"], "sugestoes": ["Conferir o campo G090"]})],
        [{"type": "text", "text": "Pronto: bloqueei o osso no G090 e recalculei."}],
    ])
    monkeypatch.setattr(C, "_cliente", lambda chave: falso)
    r = _ok(c.post(f"/api/{sid}/copiloto", json={"texto": "Avalie e proteja o osso"}))
    assert r["resposta"].startswith("Pronto") and r["parecer"]["aval"] == "melhorar"
    assert [a.split()[0] for a in r["acoes"]] == ["bloquear_orgao", "calcular"]
    # o que voltou para o Claude: o plano lido, e a ferramenta proibida não existe
    res = {b["tool_use_id"]: b["content"] for m in falso.chamadas[-1]["messages"] if m["role"] == "user"
           and isinstance(m["content"], list) for b in m["content"]}
    assert '"rotulo": "COPI"' in res["a"] and '"estruturas"' in res["b"]
    assert "ferramenta desconhecida" in res["d"]
    assert "aprovar" not in {t["name"] for t in falso.chamadas[0]["tools"]}
    g090 = next(x for x in r["resumo"]["plano"]["campos"] if x["nome"] == "G090")
    assert any(b["nome"] == "Osso_inserto" for b in (g090["spec"] or {}).get("blindagens", []))
    assert any("Copiloto (IA): bloquear_orgao" in h for h in r["resumo"]["log"])
    assert any(h.get("evento") == "copiloto" and h["acao"] == "bloquear_orgao" for h in r["resumo"]["plano"]["historico"])
    # desfazer volta ao plano de antes do pedido
    d = _ok(c.post(f"/api/{sid}/copiloto/desfazer"))
    g090 = next(x for x in d["plano"]["campos"] if x["nome"] == "G090")
    assert not (g090["spec"] or {}).get("blindagens")
    assert c.post(f"/api/{sid}/copiloto/desfazer").status_code == 400
    # a conversa continua (histórico enviado de novo) e "nova" começa do zero
    falso.roteiro = [[{"type": "text", "text": "ok"}]]
    _ok(c.post(f"/api/{sid}/copiloto", json={"texto": "obrigado"}))
    assert len(falso.chamadas[-1]["messages"]) > 3
    falso.roteiro = [[{"type": "text", "text": "ok"}]]
    _ok(c.post(f"/api/{sid}/copiloto", json={"texto": "outra", "novo": True}))
    assert falso.chamadas[-1]["messages"][0]["content"] == "outra"


def test_rascunho_de_maquina(cli):
    from tps.app import copiloto as CP
    ck = CP.checklist()
    assert any(b["bloco"].startswith("Dados de feixe") for b in ck["blocos"])
    r = CP.criar_rascunho_maquina({"id": "Clinica Versa", "model": "Versa HD", "mlc": "Agility",
                                   "energias_mv": [6, 10], "dicom_machine_names": ["VERSA1"]})
    raw = yaml.safe_load(open(r["pasta"] + "/maquina.yaml", encoding="utf-8"))
    assert raw["id"] == "clinica_versa" and raw["collimation"]["mlc"]["leaf_pairs"] == 80
    assert raw["collimation"]["jaw_x"]["present"] is False and not raw["rascunho"]["comissionado"]
    assert "serial" in r["pendencias"] and "collimation.mlc.min_gap_mm" in r["pendencias"]
    assert "rascunho" in r["pasta"]
    with pytest.raises(ValueError):
        CP.criar_rascunho_maquina({})


def test_aplicar_a_todos_os_campos(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "TODOS", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    for g in (0, 90, 180, 270):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g, "peso": 1.0 + g / 900}))
    r = _ok(c.post(f"/api/{sid}/campos/todos", json={"spec": {
        "alvo": "PTV_fantoma", "margem": {"x1": 7, "x2": 7, "y1": 7, "y2": 7}, "ponto_encontro": "meio",
        "blindagens": [{"nome": "Osso_inserto", "margem": 3}], "otimizar_colimador": True}}))
    assert r["ajuste_todos"]["campos"] == ["G000", "G090", "G180", "G270"] and not r["ajuste_todos"]["erros"]
    for cp in r["plano"]["campos"]:
        assert cp["spec"]["ponto_encontro"] == "meio" and cp["spec"]["blindagens"][0]["nome"] == "Osso_inserto"
        assert cp["spec"]["otimizar_colimador"] and cp["spec"]["margem"]["x1"] == 7
    pesos = {cp["nome"]: cp["spec"]["peso"] for cp in r["plano"]["campos"]}
    assert pesos["G090"] == pytest.approx(1.1) and pesos["G000"] == pytest.approx(1.0)   # cada um mantém o peso
    assert {cp["nome"]: cp["gantry"] for cp in r["plano"]["campos"]}["G270"] == 270


def test_raciocinio_de_pedidos_anteriores_nao_volta(cli, monkeypatch):
    """Os blocos thinking de pedidos anteriores saem do histórico (a assinatura deles não vale fora do pedido)."""
    from tps.planejamento import assistente_claude as C
    c, sid, _ = cli
    _ok(c.post("/api/claude", json={"chave": "sk-ant-teste-9999", "senha": SENHA}))
    pensar = {"type": "thinking", "thinking": "hmm", "signature": "abc"}
    falso = _Falso([[pensar, _tu("t1", "ler_plano", {})], [pensar, {"type": "text", "text": "um"}],
                    [{"type": "text", "text": "dois"}]])
    monkeypatch.setattr(C, "_cliente", lambda chave: falso)
    _ok(c.post(f"/api/{sid}/copiloto", json={"texto": "primeiro", "novo": True}))
    # dentro do pedido, o thinking vai intacto junto do tool_use
    assert any(b.get("type") == "thinking" for m in falso.chamadas[1]["messages"] if m["role"] == "assistant"
               for b in m["content"])
    _ok(c.post(f"/api/{sid}/copiloto", json={"texto": "segundo"}))
    hist = falso.chamadas[-1]["messages"]
    assert not any(isinstance(m["content"], list) and any(b.get("type") == "thinking" for b in m["content"])
                   for m in hist if m["role"] == "assistant")
    # um pedido que falha não deixa a conversa pela metade
    falso.roteiro = []
    assert c.post(f"/api/{sid}/copiloto", json={"texto": "terceiro"}).status_code == 400
    falso.roteiro = [[{"type": "text", "text": "ok"}]]
    _ok(c.post(f"/api/{sid}/copiloto", json={"texto": "quarto"}))
    assert [m["content"] for m in falso.chamadas[-1]["messages"] if m["role"] == "user"
            and isinstance(m["content"], str)][-2:] == ["segundo", "quarto"]


def test_copiloto_cria_plano_do_zero(cli, monkeypatch):
    from tps.planejamento import assistente_claude as C
    c, sid, _ = cli
    _ok(c.post("/api/claude", json={"chave": "sk-ant-teste-9999", "senha": SENHA}))
    falso = _Falso([
        [_tu("a", "listar_opcoes", {})],
        [_tu("b", "criar_plano", {"rotulo": "COPI3D", "dose_total_gy": 50, "fracoes": 25, "alvo": "PTV_fantoma"})],
        [_tu("c", "criar_campos", {"tecnica": "3D", "gantries": [0, 120, 240], "margem_mm": 7})],
        [_tu("d", "calcular", {})],
        [{"type": "text", "text": "Plano feito."}]])
    monkeypatch.setattr(C, "_cliente", lambda chave: falso)
    r = _ok(c.post(f"/api/{sid}/copiloto", json={"texto": "faça um plano 3D de 3 campos", "novo": True}))
    assert [a.split()[0] for a in r["acoes"]] == ["criar_plano", "criar_campos", "calcular"]
    assert r["resumo"]["plano"]["rotulo"] == "COPI3D" and [x["gantry"] for x in r["resumo"]["plano"]["campos"]] == [0, 120, 240]
    assert r["resumo"]["dose_atualizada"]
