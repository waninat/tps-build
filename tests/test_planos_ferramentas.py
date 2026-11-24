"""Vários planos por TC, campos de setup, FiF filtro, normalizações pós-cálculo, isodoses configuráveis e edição de
lâminas no BEV (arrasto com vizinhas e desenho). SRS-PLN/UI."""
import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_planos")
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
    sid = s["id"]
    c.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"})
    return c, sid, s


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def _caixa(c, sid, rot):
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": rot, "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    for g in (0, 90, 180, 270):
        r = _ok(c.post(f"/api/{sid}/campo", json={"gantry": g}))
    return r


def test_varios_planos_na_mesma_tc(cli):
    c, sid, _ = cli
    _caixa(c, sid, "A")
    _ok(c.post(f"/api/{sid}/calcular", json={}))
    r = c.post(f"/api/{sid}/plano", json={"rotulo": "A", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"})
    assert r.status_code == 400                                   # rótulo repetido
    r = _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "B", "dose_total": 45, "fracoes": 25, "alvo": "PTV_fantoma"}))
    assert {q["rotulo"] for q in r["planos_sessao"]} == {"A", "B"} and r["plano"]["rotulo"] == "B"
    assert not r["plano"]["campos"]
    r = _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "A"}))
    assert r["plano"]["rotulo"] == "A" and len(r["plano"]["campos"]) == 4 and r["dose_atualizada"]
    r = _ok(c.post(f"/api/{sid}/plano/copiar", json={"rotulo": "A2"}))
    assert r["plano"]["rotulo"] == "A2" and len(r["plano"]["campos"]) == 4
    r = _ok(c.delete(f"/api/{sid}/plano/B"))
    assert {q["rotulo"] for q in r["planos_sessao"]} == {"A", "A2"}
    sv = _ok(c.post(f"/api/{sid}/salvar"))
    assert sv["arquivo"]


def test_normalizacoes_e_isodoses(cli):
    c, sid, s = cli
    _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "A"}))
    r = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "maximo"}))
    rx = 50.0
    assert r["resultado"]["dmax_gy"] == pytest.approx(rx, rel=2e-3)
    assert len(r["resultado"]["dmax_xyz"]) == 3
    mu_max = sum(q["mu"] for q in r["plano"]["campos"])
    # prescrever na isodose de 95%: o máximo vai a 100/95 da prescrição, sem recalcular a dose
    r = _ok(c.post(f"/api/{sid}/calcular", json={"so_normalizar": True, "metodo": "maximo", "isodose_pct": 95}))
    assert r["resultado"]["dmax_gy"] == pytest.approx(rx / 0.95, rel=2e-3)
    assert sum(q["mu"] for q in r["plano"]["campos"]) == pytest.approx(mu_max / 0.95, rel=3e-3)
    r = _ok(c.post(f"/api/{sid}/calcular", json={"so_normalizar": True, "metodo": "cobertura", "isodose_pct": 100,
                                                  "cobertura_volume_pct": 95, "cobertura_dose_pct": 100}))
    assert r["resultado"]["estatisticas"]["PTV_fantoma"]["D95"] == pytest.approx(rx, rel=5e-3) \
        if "estatisticas" in r["resultado"] else True
    dv = _ok(c.get(f"/api/{sid}/dvh"))["PTV_fantoma"]
    d, v = np.array(dv["dose"]), np.array(dv["volume"])
    assert np.interp(-95.0, -v, d) == pytest.approx(rx, rel=0.01)       # D95 = prescrição
    k = s["ct"]["z"][2] // 2
    sob = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{k}?ref=max&unidade=gy&niveis=40,45,50"))
    assert sob["iso_ref"]["unidade"] == "gy" and sob["iso_ref"]["niveis"] == [40, 45, 50]
    assert sob["iso_ref"]["cem_gy"] == pytest.approx(sob["iso_ref"]["dmax_gy"])
    sob = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{k}?niveis=95,105"))
    assert [q["nivel"] for q in sob["isodoses"]] <= [95, 105]


def test_fif_filtro(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "A2"}))
    r0 = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo", "isodose_pct": 100}))
    nomes_lat = [q["nome"] for q in r0["plano"]["campos"] if round(q["gantry"]) in (90, 270)]
    r = _ok(c.post(f"/api/{sid}/calcular", json={"field_in_field": {"modo": "filtro", "niveis": [97, 94, 91, 88],
                                                                       "peso_pct": 3, "campos": nomes_lat}}))
    info = r["resultado"]["info"]["field_in_field"]
    assert info["modo"] == "filtro" and info["subcampos"]
    assert all(any(n.startswith(p) for p in nomes_lat) for n in info["subcampos"])
    # fantoma homogêneo (HI ~0,04): o filtro não tem o que achatar; só não pode estragar. Na pelve, HI 0,098 → 0,057.
    assert info["hi_depois"] < 1.5 * info["hi_antes"]


def test_setup_exportado_como_setup(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "A"}))
    _ok(c.post(f"/api/{sid}/setup", json={"nome": "SETUP AP", "gantry": 0, "x_cm": 20, "y_cm": 20}))
    r = _ok(c.post(f"/api/{sid}/setup", json={"nome": "SETUP LAT", "gantry": 90, "x_cm": 15, "y_cm": 20}))
    assert [q["nome"] for q in r["plano"]["campos_setup"]] == ["SETUP AP", "SETUP LAT"]
    assert len(r["plano"]["campos"]) == 4                         # o setup não entra nos campos de tratamento
    _ok(c.get(f"/api/{sid}/bev/SETUP%20LAT"))
    _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo"}))
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1"}))
    ex = _ok(c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1", "rtstruct": False}))
    rp = pydicom.dcmread(next(a for a in ex["arquivos"] if "RP_" in a))
    tipos = {b.BeamName: b.TreatmentDeliveryType for b in rp.BeamSequence}
    assert tipos["SETUP AP"] == "SETUP" and tipos["SETUP LAT"] == "SETUP"
    assert sum(1 for t in tipos.values() if t == "TREATMENT") == 4
    fg = rp.FractionGroupSequence[0]
    assert fg.NumberOfBeams == 4 and len(fg.ReferencedBeamSequence) == 4
    _ok(c.post(f"/api/{sid}/desaprovar", json={"senha": "senha-forte-1", "motivo": "teste de setup"}))
    r = _ok(c.delete(f"/api/{sid}/setup/SETUP%20AP"))
    assert [q["nome"] for q in r["plano"]["campos_setup"]] == ["SETUP LAT"]


def test_edicao_de_laminas_no_bev(cli):
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "A2"}))
    bev = _ok(c.get(f"/api/{sid}/bev/G090"))
    a0, b0 = bev["campo"]["mlc_a"], bev["campo"]["mlc_b"]
    abertos = [i for i in range(len(a0)) if b0[i] - a0[i] > 6]
    i = abertos[len(abertos) // 2]
    # A do par passa muito além da B do vizinho: o MLCi não interdigita, então a vizinha é empurrada (não recusa)
    alvo = b0[i + 1] + 10
    r = _ok(c.post(f"/api/{sid}/campo/G090/lamina", json={"par": i, "banco": "a", "pos": alvo}))
    cp = next(q for q in r["plano"]["campos"] if q["nome"] == "G090")
    assert cp["mlc_a"][i] == pytest.approx(alvo, abs=0.11)
    assert cp["mlc_b"][i] >= alvo + 5 - 1e-6                     # a B do próprio par recua (gap mínimo)
    assert cp["mlc_b"][i + 1] >= cp["mlc_a"][i] + 5 - 1e-6
    r = _ok(c.post(f"/api/{sid}/campo/G090/lamina", json={"jaw": "y2", "pos": 30}))
    cp = next(q for q in r["plano"]["campos"] if q["nome"] == "G090")
    assert cp["jaw_y"][1] == pytest.approx(30)
    # desenho: as lâminas vão para o quadrado desenhado
    q = [[-30, -30], [30, -30], [30, 30], [-30, 30]]
    r = _ok(c.post(f"/api/{sid}/campo/G090/desenho", json={"pontos": q, "modo": "abrir", "encontro": "fora"}))
    cp = next(x for x in r["plano"]["campos"] if x["nome"] == "G090")
    assert cp["jaw_y"] == pytest.approx([-30, 30], abs=0.6)
    abertos = [k for k in range(len(cp["mlc_a"])) if cp["mlc_b"][k] - cp["mlc_a"][k] > 6]
    assert abertos and all(cp["mlc_a"][k] == pytest.approx(-30, abs=0.6) and cp["mlc_b"][k] == pytest.approx(30, abs=0.6)
                           for k in abertos)
    r = c.post(f"/api/{sid}/campo/G090/desenho", json={"pontos": [[0, 0], [1, 1]], "modo": "abrir"})
    assert r.status_code == 400


def test_soma_e_comparacao_de_planos(cli):
    c, sid, s = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "FASE1", "dose_total": 46, "fracoes": 23, "alvo": "PTV_fantoma"}))
    for g in (0, 180):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g}))
    _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo"}))
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "BOOST", "dose_total": 14, "fracoes": 7, "alvo": "PTV_fantoma"}))
    for g in (90, 270):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g}))
    _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo"}))
    r = _ok(c.post(f"/api/{sid}/soma_planos", json={"rotulos": ["FASE1", "BOOST"], "modo": "fisica"}))
    assert r["rx_gy"] == pytest.approx(60)
    assert r["estatisticas"]["PTV_fantoma"]["Dmean"] == pytest.approx(60, rel=0.01)
    e = _ok(c.post(f"/api/{sid}/soma_planos", json={"rotulos": ["FASE1", "BOOST"], "modo": "eqd2", "ab_alvo": 10}))
    assert e["rx_gy"] == pytest.approx(60, rel=1e-6)                         # 2 Gy/fx: EQD2 = dose física
    h = _ok(c.post(f"/api/{sid}/soma_planos", json={"rotulos": ["FASE1", "BOOST"], "modo": "eqd2", "ab_alvo": 3}))
    assert h["rx_gy"] == pytest.approx(60, rel=1e-6)
    k = s["ct"]["z"][2] // 2
    sob = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{k}?fonte=soma"))
    assert sob["iso_ref"]["fonte"] == "soma" and sob["iso_ref"]["rx_gy"] == pytest.approx(60)
    assert c.get(f"/api/{sid}/dose_corte/axial/{k}.png?fonte=soma").status_code == 200
    cp = _ok(c.post(f"/api/{sid}/comparar_planos", json={"a": "FASE1", "b": "BOOST"}))
    assert cp["tabela"]["PTV_fantoma"]["Dmean"][0] == pytest.approx(46, rel=0.01)
    assert cp["tabela"]["PTV_fantoma"]["Dmean"][1] == pytest.approx(14, rel=0.01)
    assert cp["dif_min_gy"] < -20
    sob = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{k}?fonte=dif"))
    assert sob["iso_ref"]["dif"] and any(q["nivel"] < 0 for q in sob["isodoses"])
    assert c.get(f"/api/{sid}/dose_corte/axial/{k}.png?fonte=dif").status_code == 200
    assert c.post(f"/api/{sid}/soma_planos", json={"rotulos": ["FASE1"]}).status_code == 400


def test_checklist_detecta_colisao_e_normalizacao(cli):
    from tps.app import checklist as CK
    from tps.app.servidor import SESSOES
    c, sid, _ = cli
    _ok(c.post(f"/api/{sid}/plano/trocar", json={"rotulo": "FASE1"}))
    lista = {i["id"]: i for i in _ok(c.get(f"/api/{sid}/checklist"))}
    assert lista["dose"]["estado"] == "ok" and lista["normalizacao"]["estado"] == "ok"
    assert lista["colisao"]["estado"] == "ok" and lista["isocentro"]["estado"] == "ok"
    s = SESSOES[sid]
    antes = CK.FOLGA_CABECOTE_MM
    try:
        CK.FOLGA_CABECOTE_MM = 50.0                       # cabeçote "encostando": todo campo colide
        assert CK.colisoes(s)
    finally:
        CK.FOLGA_CABECOTE_MM = antes
    s.plano.prescricao.isodose_pct = 90.0                 # prescrição mexida sem recalcular
    try:
        lista = {i["id"]: i for i in _ok(c.get(f"/api/{sid}/checklist"))}
        assert lista["normalizacao"]["estado"] == "erro"
    finally:
        s.plano.prescricao.isodose_pct = 100.0
