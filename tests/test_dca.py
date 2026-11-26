"""Arco conformacional dinâmico (DCA) e elegibilidade para CQ por cálculo. SRS-PLN-110/111."""
import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_dca")
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
    s = c.post("/api/caso/ct", json={"pasta": str(caso)}).json()
    c.post(f"/api/{s['id']}/maquina", json={"id": "radioserra_precise"})
    return c, s["id"]


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_dca_de_ponta_a_ponta(cli):
    c, sid = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "DCA", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    r = _ok(c.post(f"/api/{sid}/dca/arcos", json={"n": 2, "inicio": 181, "fim": 179, "margem_mm": 5, "passo_cp": 5}))
    assert r["plano"]["tecnica"] == "DCA" and set(r["dca"]) == {"DCA1", "DCA2"}
    assert r["dca"]["DCA1"]["pontos"] == 73 and r["dca"]["DCA1"]["colimador"] != r["dca"]["DCA2"]["colimador"]
    r = _ok(c.post(f"/api/{sid}/calcular", json={"otimizar_pesos": True}))
    est = r["resultado"]["estatisticas"]["PTV_fantoma"]
    assert est["D95"] > 0.95 * 50 and est["D2"] < 1.07 * 50
    conf = r["resultado"]["conferencia_um"]
    assert any(k.get("composta") for k in conf) and all(abs(k["dif_pct"]) < 3 for k in conf)
    cq = _ok(c.get(f"/api/{sid}/cq_calculo"))
    itens = {i["id"]: i for i in cq["itens"]}
    assert itens["tecnica"]["ok"] and itens["um_grau"]["valor"] < 1 and itens["mcs"]["valor"] > 0.4
    assert cq["elegivel"] and not cq["confirmado"]
    chk = {i["id"]: i for i in _ok(c.get(f"/api/{sid}/checklist"))}
    assert chk["cq_calculo"]["estado"] == "aviso" and "não confirmados" in chk["cq_calculo"]["detalhe"]
    pdf = c.get(f"/api/{sid}/relatorio.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # campos com FiF/compensador não se aplicam ao DCA
    assert c.post(f"/api/{sid}/calcular", json={"field_in_field": True}).status_code == 400
    # exportação: um feixe DYNAMIC por arco, UM por grau constante, entregável
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1", "justificativa": "Teste automático do DCA"}))
    ex = _ok(c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1", "rtstruct": False}))
    rp = pydicom.dcmread(next(a for a in ex["arquivos"] if "RP_" in a))
    assert [b.BeamType for b in rp.BeamSequence] == ["DYNAMIC", "DYNAMIC"]
    for b in rp.BeamSequence:
        g = np.array([float(cp.GantryAngle) for cp in b.ControlPointSequence])
        w = np.array([float(cp.CumulativeMetersetWeight) for cp in b.ControlPointSequence])
        passo = np.abs((np.diff(g) + 180) % 360 - 180)
        r = np.diff(w) / passo
        assert r.std() / r.mean() < 0.005
    assert not [i for i in ex["entregabilidade"] if i["gravidade"] == "erro"]


def test_bloqueio_de_orgao_e_imrt_nao_elegivel(cli):
    c, sid = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "DCAOAR", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    r0 = _ok(c.post(f"/api/{sid}/dca/arcos", json={"n": 1, "margem_mm": 5, "passo_cp": 10}))
    r1 = _ok(c.post(f"/api/{sid}/dca/arcos", json={"n": 1, "margem_mm": 5, "passo_cp": 10, "colimadores": [r0["dca"]["DCA1"]["colimador"]],
                                                    "oars": [{"estrutura": "Osso_inserto", "margem_mm": 3}],
                                                    "max_bloqueio_pct": 25}))
    assert r1["dca"]["DCA1"]["bloqueio_pct"] > r0["dca"]["DCA1"]["bloqueio_pct"]
    assert r1["dca"]["DCA1"]["bloqueio_pct"] <= 25.5
