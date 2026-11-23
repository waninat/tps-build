"""Restrições próprias, operação booleana no contorno, órgãos por limiar, modelos de plano por sítio, proteção de
OARs e ajuste fino depois do filtro. SRS-PLN-070..086."""
import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from tps import fantoma, paths
from tps.planejamento import contorno as K
from tps.planejamento import modelos_plano as MP


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_modelos")
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
    return c, s["id"], home


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_modelos_carregam_e_acham_estruturas():
    mods = MP.carregar()
    assert {"colo_uterino", "reto", "prostata", "mama_e", "mama_d", "pulmao", "esofago"} <= set(mods)
    nomes = ["BODY", "PTV_45", "BEXIGA", "Reto", "Cabeca femoral esq", "CTV"]
    assert MP.achar_estrutura(nomes, mods["colo_uterino"]["alvo"]) == "PTV_45"
    assert MP.achar_todas(nomes, mods["colo_uterino"]["oars"]) == ["BEXIGA", "Reto", "Cabeca femoral esq"]


def test_modelo_box_com_filtro_e_cobertura(cli):
    c, sid, _ = cli
    assert any(m["id"] == "colo_uterino" for m in _ok(c.get("/api/modelos_plano")))
    r = _ok(c.post(f"/api/{sid}/modelo_plano", json={"modelo": "colo_uterino", "rotulo": "COLO"}))
    p = r["plano"]
    assert p["prescricao"]["metodo"] == "cobertura" and p["prescricao"]["dose_total_gy"] == 45
    principais = [q for q in p["campos"] if not q["campo_pai"]]
    assert sorted(round(q["gantry"]) for q in principais) == [0, 90, 180, 270]
    subs = [q for q in p["campos"] if q["campo_pai"]]
    assert subs and {q["campo_pai"] for q in subs} <= {"LE", "LD"}
    dv = _ok(c.get(f"/api/{sid}/dvh"))["PTV_fantoma"]
    assert np.interp(-95.0, -np.array(dv["volume"]), np.array(dv["dose"])) == pytest.approx(45, rel=0.01)


def test_tangentes_poupam_pulmao():
    # fantoma: "mama" à esquerda, anterior; "pulmão" posterior a ela
    x = y = np.arange(-150, 151, 4.0)
    z = np.arange(-40, 41, 4.0)
    Z, Y, X = np.meshgrid(z, y, x, indexing="ij")
    mama = ((X - 60) ** 2 / 50 ** 2 + (Y + 60) ** 2 / 30 ** 2 <= 1) & (np.abs(Z) < 30)
    pulmao = ((X - 60) ** 2 / 60 ** 2 + (Y - 20) ** 2 / 40 ** 2 <= 1) & (np.abs(Z) < 40)
    t = MP.tangentes(mama, pulmao, (x, y, z), (60.0, -60.0, 0.0), "E")
    assert 290 <= t["medial"] <= 330
    assert abs(((t["lateral"] - t["medial"]) % 360) - 180) < 10
    ruim = MP.tangentes(mama, pulmao, (x, y, z), (60.0, -60.0, 0.0), "E", passo=40)   # só os extremos
    assert t["fracao_pulmao"] <= ruim["fracao_pulmao"]


def test_restricao_propria_e_protocolo(cli):
    c, sid, home = cli
    _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "COLO"}))
    r = c.post(f"/api/{sid}/restricao", json={"estrutura": "Osso_inserto", "metrica": "D2xx", "op": "<=", "valor": 40})
    assert r.status_code == 400
    r = _ok(c.post(f"/api/{sid}/restricao", json={"estrutura": "Osso_inserto", "metrica": "Dmean", "op": "<=",
                                                   "valor": "30,5", "tipo": "objetivo"}))
    rr = r["resultado"]["restricoes"]["resultados"]
    propria = [q for q in rr if q.get("extra") == 0]
    assert propria and propria[0]["limite"] == 30.5 and propria[0]["metrica"] == "Dmean"
    r = _ok(c.delete(f"/api/{sid}/restricao/0"))
    assert not any(q.get("extra") is not None for q in r["resultado"]["restricoes"]["resultados"])
    # no protocolo da clínica: só com nome TG-263 reconhecido
    r = c.post(f"/api/{sid}/restricao", json={"estrutura": "Osso_inserto", "metrica": "Dmax", "valor": 50,
                                               "na_clinica": True})
    assert r.status_code == 400 and "TG-263" in r.json()["erro"]


def test_operar_contorno_subtrair_com_margem(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/contorno/nova", json={"nome": "CAIXA_OAR"}))
    s = _ok(c.get(f"/api/{sid}"))
    k0 = s["ct"]["z"][2] // 2
    for k in range(k0 - 6, k0 + 7):
        _ok(c.post(f"/api/{sid}/contorno/editar", json={"nome": "CAIXA_OAR", "idx": k, "ferramenta": "poligono",
                                                        "pontos": [[-80, -60], [80, -60], [80, 60], [-80, 60]]}))
    v0 = next(e["volume_cc"] for e in _ok(c.get(f"/api/{sid}"))["estruturas"] if e["nome"] == "CAIXA_OAR")
    r = _ok(c.post(f"/api/{sid}/contorno/operar", json={"nome": "CAIXA_OAR", "operacao": "subtrair",
                                                        "outra": "PTV_fantoma", "margem_mm": 5}))
    v1 = r["contorno"]["volume_cc"]
    assert v1 < v0
    vptv = next(e["volume_cc"] for e in r["estruturas"] if e["nome"] == "PTV_fantoma")
    assert v0 - v1 > 0.3 * min(vptv, v0)
    _ok(c.post(f"/api/{sid}/contorno/desfazer", json={}))
    v2 = next(e["volume_cc"] for e in _ok(c.get(f"/api/{sid}"))["estruturas"] if e["nome"] == "CAIXA_OAR")
    assert v2 == pytest.approx(v0, rel=0.02)


def test_pulmoes_por_limiar():
    x = y = np.arange(-160, 161, 2.0)
    nz = 10
    hu = np.full((nz, len(y), len(x)), -1000.0)
    Y, X = np.meshgrid(y, x, indexing="ij")
    corpo2 = X ** 2 / 150 ** 2 + Y ** 2 / 110 ** 2 <= 1
    hu[:, corpo2] = 30
    for xc in (-70, 70):
        hu[:, (X - xc) ** 2 / 45 ** 2 + Y ** 2 / 70 ** 2 <= 1] = -800
    hu[:, (X ** 2 + (Y + 60) ** 2) <= 8 ** 2] = -950          # traqueia na linha média
    corpo = K.corpo_3d(hu)
    esq, dire = K.pulmoes_automaticos(hu, corpo, x, y, 3.0)
    v = 2 * 2 * 3 / 1000
    ref = np.pi * 45 * 70 * 30 / 1000
    assert esq.sum() * v == pytest.approx(ref, rel=0.1) and dire.sum() * v == pytest.approx(ref, rel=0.1)
    assert X[esq[0]].min() > 0 and X[dire[0]].max() < 0
    assert not esq[:, (X ** 2 + (Y + 60) ** 2) <= 6 ** 2].any()


def test_protecao_de_oar_baixa_a_dose_do_orgao(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "OAR", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    for g in (0, 90, 180, 270):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g}))
    r0 = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo"}))
    m0 = r0["resultado"]["estatisticas"]["CAIXA_OAR"]["Dmean"]
    r = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo", "proteger_oars": {
        "oars": [{"estrutura": "CAIXA_OAR", "metrica": "Dmean", "valor": None, "prioridade": 8}]}}))
    info = r["resultado"]["info"]["protecao_oars"]
    m1 = r["resultado"]["estatisticas"]["CAIXA_OAR"]["Dmean"]
    assert m1 < m0
    assert info["oars"]["CAIXA_OAR"]["depois"]["Dmean"] < info["oars"]["CAIXA_OAR"]["antes"]["Dmean"]
    assert r["resultado"]["estatisticas"]["PTV_fantoma"]["D95"] > 0.9 * 50


def test_filtro_com_ajuste_fino_nao_piora(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "COLO"}))
    p = _ok(c.get(f"/api/{sid}"))["plano"]
    lat = [q["nome"] for q in p["campos"] if not q["campo_pai"] and round(q["gantry"]) in (90, 270)]
    r = _ok(c.post(f"/api/{sid}/calcular", json={"field_in_field": {"modo": "filtro", "campos": lat,
                                                                       "ajuste_fino": True}}))
    i = r["resultado"]["info"]["field_in_field"]
    assert "ajuste_fino" in i and i["ajuste_fino"]["hi_depois"] <= i["ajuste_fino"]["hi_antes"] + 1e-9
