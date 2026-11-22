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
    c = TestClient(app, raise_server_exceptions=False)
    assert c.get("/api/info").json()["configurar"]
    r = c.post("/api/configurar", json={"login": "fisico.teste", "nome": "Físico Teste", "senha": "senha-forte-1"})
    c.headers["X-Token"] = r.json()["token"]
    return c, caso, home


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
    cf = c.get(f"/api/{sid}/checkfilm.pdf")
    assert cf.status_code == 200 and cf.content[:4] == b"%PDF"
    est = _ok(c.post(f"/api/{sid}/estrutura", json={"nome": "PTV_mais5", "operacao": "margem", "a": "PTV_fantoma",
                                                     "margens": {"direita": 5, "esquerda": 5, "anterior": 5,
                                                                 "posterior": 5, "superior": 10, "inferior": 0}}))
    vols = {e["nome"]: e["volume_cc"] for e in est["estruturas"]}
    assert vols["PTV_mais5"] > vols["PTV_fantoma"]
    est = _ok(c.post(f"/api/{sid}/estrutura", json={"nome": "ANEL", "operacao": "anel", "a": "PTV_fantoma",
                                                     "interno_mm": 0, "externo_mm": 15}))
    est = _ok(c.post(f"/api/{sid}/estrutura", json={"nome": "ISO45", "operacao": "isodose", "a": "", "nivel_gy": 45}))
    assert {"PTV_mais5", "ANEL", "ISO45"} <= set(_ok(c.get(f"/api/{sid}/dvh")))
    r = c.post(f"/api/{sid}/estrutura", json={"nome": "ANEL", "operacao": "anel", "a": "PTV_fantoma", "externo_mm": 5})
    assert r.status_code == 400
    _ok(c.delete(f"/api/{sid}/estrutura/ISO45"))
    assert c.delete(f"/api/{sid}/estrutura/PTV_fantoma").status_code == 400
    rr = _ok(c.get(f"/api/{sid}/restricoes"))
    assert any(q["tg263"] == "alvo" and q["metrica"] == "V95%" for q in rr["resultados"])
    assert s["resultado"]["restricoes"]["protocolo"]
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
    assert c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1"}).status_code == 400
    assert c.post(f"/api/{sid}/aprovar", json={"senha": "errada"}).status_code == 401
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1"}))
    assert c.post(f"/api/{sid}/campo", json={"gantry": 45}).status_code == 400      # bloqueado
    assert c.post(f"/api/{sid}/exportar", json={}).status_code == 401                # exige senha
    ex = _ok(c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1"}))
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
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1"}))
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
        ex = _ok(c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1", "enviar": {
            "host": "127.0.0.1", "porta": 11199, "ae_destino": "MOSAIQ_TESTE"}}))
    finally:
        srv.shutdown()
    # 2D agora sai com CT virtual (geometria de paciente): RTPLAN + RTDOSE + CT + RTSTRUCT
    assert recebidos[0] == "RTPLAN" and {"RTDOSE", "CT", "RTSTRUCT"} <= set(recebidos)
    assert all(e["status"] == 0 for e in ex["envio"]["enviados"])
    rp = pydicom.dcmread(ex["arquivos"][0])
    assert rp.RTPlanGeometry == "PATIENT"
    from pathlib import Path as _P
    ct = [f for f in ex["arquivos"] if "imagens_" in f and _P(f).name.startswith("CT")][0]
    d = pydicom.dcmread(ct)
    assert d.FrameOfReferenceUID == rp.FrameOfReferenceUID and "FANTOMA VIRTUAL" in d.SeriesDescription


def test_sem_login_e_papeis(cli):
    c, caso, home = cli
    anonimo = TestClient(c.app, raise_server_exceptions=False)
    assert anonimo.post("/api/caso/ct", json={"pasta": str(caso)}).status_code == 401
    assert anonimo.post("/api/configurar", json={"login": "x", "nome": "x", "senha": "12345678"}).status_code == 401
    _ok(c.post("/api/usuarios", json={"login": "dosi", "nome": "Dosimetrista", "papel": "dosimetrista",
                                      "senha": "senha-dosi-1", "senha_admin": "senha-forte-1"}))
    d = TestClient(c.app, raise_server_exceptions=False)
    d.headers["X-Token"] = _ok(d.post("/api/login", json={"login": "dosi", "senha": "senha-dosi-1"}))["token"]
    s = _ok(d.post("/api/caso/fantoma", json={"tipo": "caixa", "largura": 300, "espessura": 200, "comprimento": 200}))
    sid = s["id"]
    _ok(d.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"}))
    _ok(d.post(f"/api/{sid}/plano", json={"rotulo": "P", "dose_total": 8, "fracoes": 1, "alvo": "", "metodo": "ponto",
                                          "ponto": [0, 0, 0]}))
    _ok(d.post(f"/api/{sid}/campo", json={"nome": "AP", "gantry": 0, "modo": "retangular", "x1": 50, "x2": 50,
                                          "y1": 50, "y2": 50}))
    _ok(d.post(f"/api/{sid}/calcular", json={"metodo": "ponto", "ponto": [0, 0, 0]}))
    r = d.post(f"/api/{sid}/aprovar", json={"senha": "senha-dosi-1"})
    assert r.status_code == 401 and "não pode" in r.json()["erro"]
    # senhas erradas bloqueiam
    for _ in range(5):
        d.post("/api/login", json={"login": "dosi", "senha": "errada"})
    assert "bloqueado" in d.post("/api/login", json={"login": "dosi", "senha": "senha-dosi-1"}).json()["erro"]
    a = _ok(c.get("/api/auditoria"))
    assert a["verificacao"]["integra"] and any(x["acao"] == "senha_errada" for x in a["registros"])


def test_auditoria_detecta_adulteracao(cli):
    c, caso, home = cli
    import sqlite3
    con = sqlite3.connect(home / "tps.db")
    con.execute("UPDATE auditoria SET usuario='outro' WHERE id=(SELECT MIN(id) FROM auditoria)")
    con.commit()
    con.close()
    assert not _ok(c.get("/api/auditoria"))["verificacao"]["integra"]


def test_densidade_bolus_pontos_e_pdf(cli):
    c, caso, home = cli
    s = _ok(c.post("/api/caso/fantoma", json={"tipo": "caixa", "largura": 300, "espessura": 200, "comprimento": 200,
                                               "paciente_id": "B1"}))
    sid = s["id"]
    _ok(c.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"}))
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "BOL", "dose_total": 10, "fracoes": 5, "alvo": "",
                                          "metodo": "ponto", "ponto": [0, -50, 0]}))
    _ok(c.post(f"/api/{sid}/campo", json={"nome": "AP", "gantry": 0, "modo": "retangular", "x1": 50, "x2": 50,
                                          "y1": 50, "y2": 50}))
    _ok(c.post(f"/api/{sid}/ponto", json={"nome": "PELE", "xyz": [0, -99, 0]}))
    s0 = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "ponto", "ponto": [0, -50, 0]}))
    pele0 = s0["plano"]["pontos"][0]["total_gy"]
    s = _ok(c.post(f"/api/{sid}/bolus", json={"espessura_mm": 10, "campos": ["AP"]}))
    assert s["plano"]["bolus"][0]["volume_cc"] > 50 and not s["dose_atualizada"]
    assert s["plano"]["campos"][0]["ssd_mm"] == pytest.approx(940.0, abs=3.0)          # pele subiu 1 cm (era 950)
    s1 = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "ponto", "ponto": [0, -50, 0]}))
    pele1 = s1["plano"]["pontos"][0]["total_gy"]
    assert pele1 > 1.15 * pele0                                                         # bolus tira o build-up
    # densidade forçada: pulmão (0,25) na metade de cima reduz a atenuação → mais dose embaixo
    um_agua = s1["plano"]["campos"][0]["mu"]
    _ok(c.post(f"/api/{sid}/densidade", json={"estrutura": "CORPO", "valor": 0.5}))
    s2 = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "ponto", "ponto": [0, -50, 0]}))
    assert s2["plano"]["campos"][0]["mu"] < um_agua
    d = _ok(c.get(f"/api/{sid}/dose", params={"x": 0, "y": -50, "z": 0}))
    assert d["total_gy"] == pytest.approx(10.0, rel=3e-3) and "AP" in d["campos"]
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1"}))
    pdf = c.get(f"/api/{sid}/relatorio.pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-" and len(pdf.content) > 30000
    # salvar e reabrir: bolus e densidade voltam e o plano aprovado continua íntegro
    arq = _ok(c.post(f"/api/{sid}/salvar"))["arquivo"]
    s3 = _ok(c.post("/api/caso/abrir", json={"arquivo": arq}))
    assert s3["plano"]["estado"] == "APROVADO" and s3["plano"]["bolus"] and s3["plano"]["densidades"]
    assert c.post(f"/api/{s3['id']}/exportar", json={"senha": "senha-forte-1"}).status_code == 400   # sem dose
    s4 = _ok(c.post(f"/api/{s3['id']}/calcular", json={}))
    assert s4["dose_atualizada"] and s4["plano"]["estado"] == "APROVADO"
    assert s4["plano"]["pontos"][0]["total_gy"] == pytest.approx(s2["plano"]["pontos"][0]["total_gy"], rel=1e-4)
    assert len(_ok(c.post(f"/api/{s3['id']}/exportar", json={"senha": "senha-forte-1"}))["arquivos"]) > 3
    assert any(p["rotulo"] == "BOL" for p in _ok(c.get("/api/planos")))
