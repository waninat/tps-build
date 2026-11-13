"""Interface: fluxo completo pela API (o mesmo que a tela usa) — SRS-UI-001..040."""
import threading

import pydicom
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    paths.set_home(home)
    paths.ensure_home()
    from tps.app.servidor import app
    caso = home / "fantoma"
    fantoma.gerar_exportacao(caso, calcular_dose=False)
    for f in ("RP.dcm", "RD.dcm"):
        (caso / f).unlink(missing_ok=True)
    return TestClient(app, raise_server_exceptions=False), caso, home


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_fluxo_3d_completo_pela_api(cli):
    c, caso, home = cli
    info = _ok(c.get("/api/info"))
    assert any(m["id"] == "radioserra_precise" for m in info["maquinas"])
    s = _ok(c.post("/api/caso/ct", json={"pasta": str(caso)}))
    sid = s["id"]
    assert {"PTV_fantoma", "BODY"} <= {e["nome"] for e in s["estruturas"]} or len(s["estruturas"]) >= 2
    _ok(c.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"}))
    r = c.post(f"/api/{sid}/plano", json={"rotulo": "CAIXA", "dose_total": 50, "fracoes": 25, "alvo": "NAO_EXISTE"})
    assert r.status_code == 400 and "não existe" in r.json()["erro"]
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "CAIXA", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    for g in (0, 90, 180, 270):
        s = _ok(c.post(f"/api/{sid}/campo", json={"gantry": g, "alvo": "PTV_fantoma", "margem": {"x1": 7, "x2": 7,
                                                  "y1": 10, "y2": 10}, "ponto_encontro": "area"}))
    assert len(s["plano"]["campos"]) == 4 and not s["dose_atualizada"]
    # edição manual de lâmina com violação mecânica é recusada
    c0 = s["plano"]["campos"][0]
    a, b = list(c0["mlc_a"]), list(c0["mlc_b"])
    a[20] = b[20] + 5
    r = c.post(f"/api/{sid}/campo/G000/laminas", json={"mlc_a": a, "mlc_b": b})
    assert r.status_code == 400
    s = _ok(c.post(f"/api/{sid}/calcular", json={"otimizar_pesos": True}))
    assert s["dose_atualizada"] and all(k["ok"] for k in s["resultado"]["conferencia_um"])
    assert s["resultado"]["estatisticas"]["PTV_fantoma"]["D95"] > 47.0
    assert len(_ok(c.get(f"/api/{sid}/dvh"))) >= 2
    ov = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{s['ct']['z'][2] // 2}"))
    assert ov["isodoses"] and ov["contornos"] and len(ov["feixes"]) == 4
    for vista in ("coronal", "sagital"):
        assert _ok(c.get(f"/api/{sid}/sobreposicao/{vista}/30"))["contornos"]
    r = c.get(f"/api/{sid}/corte/axial/10.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    bev = _ok(c.get(f"/api/{sid}/bev/G090"))
    assert bev["projecoes"] and len(bev["limites"]) == 41
    assert c.get(f"/api/{sid}/drr/G090.png").content[:4] == b"\x89PNG"
    # exportar exige aprovação
    assert c.post(f"/api/{sid}/exportar", json={}).status_code == 400
    _ok(c.post(f"/api/{sid}/aprovar", json={"usuario": "fisico.teste"}))
    assert c.post(f"/api/{sid}/campo", json={"gantry": 45}).status_code == 400      # bloqueado
    ex = _ok(c.post(f"/api/{sid}/exportar", json={}))
    assert len(ex["arquivos"]) == 2
    assert pydicom.dcmread(ex["arquivos"][0]).ApprovalStatus == "APPROVED"
    assert c.get(f"/api/{sid}/arquivo", params={"caminho": ex["arquivos"][0]}).status_code == 200
    assert c.get(f"/api/{sid}/arquivo", params={"caminho": "/etc/passwd"}).status_code == 400
    assert "Conferência independente" in c.get(f"/api/{sid}/relatorio").text
    salvo = _ok(c.post(f"/api/{sid}/salvar"))["arquivo"]
    s2 = _ok(c.post("/api/caso/abrir", json={"arquivo": salvo}))
    assert s2["plano"]["estado"] == "APROVADO" and len(s2["plano"]["campos"]) == 4


def test_plano_2d_com_compensador_e_envio_dicom(cli):
    c, caso, home = cli
    s = _ok(c.post("/api/caso/fantoma", json={"tipo": "elipse", "largura": 340, "espessura": 220,
                                               "comprimento": 300, "paciente_id": "T2D", "paciente_nome": "TESTE^2D"}))
    sid = s["id"]
    _ok(c.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"}))
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "AP_PA", "dose_total": 20, "fracoes": 5, "alvo": "",
                                          "metodo": "ponto", "ponto": [0, 0, 0]}))
    for g, n in ((0, "AP"), (180, "PA")):
        _ok(c.post(f"/api/{sid}/campo", json={"nome": n, "gantry": g, "modo": "retangular",
                                              "x1": 50, "x2": 50, "y1": 75, "y2": 75}))
    s = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "ponto", "ponto": [0, 0, 0]}))
    assert all(abs(k["dif_pct"]) < 2 for k in s["resultado"]["conferencia_um"])
    _ok(c.post(f"/api/{sid}/aprovar", json={"usuario": "fisico.teste"}))
    # destino DICOM de teste (faz o papel do Mosaiq)
    from pynetdicom import AE, evt, StoragePresentationContexts
    recebidos = []

    def ao_receber(ev):
        recebidos.append(ev.dataset.Modality)
        return 0x0000
    scp = AE(ae_title="MOSAIQ_TESTE")
    scp.supported_contexts = StoragePresentationContexts
    srv = scp.start_server(("127.0.0.1", 11199), block=False, evt_handlers=[(evt.EVT_C_STORE, ao_receber)])
    try:
        ex = _ok(c.post(f"/api/{sid}/exportar", json={"enviar": {"host": "127.0.0.1", "porta": 11199,
                                                                  "ae_destino": "MOSAIQ_TESTE"}}))
    finally:
        srv.shutdown()
    assert recebidos == ["RTPLAN"] and ex["envio"]["enviados"][0]["status"] == 0
