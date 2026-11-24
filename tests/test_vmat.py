"""VMAT: geometria do arco, sequenciamento em janela deslizante, mecânica, exportação e fluxo pela API. SRS-VMT-001."""
import numpy as np
import pydicom
import pytest
import yaml
from fastapi.testclient import TestClient

from tps import fantoma, paths
from tps.planejamento import calculo
from tps.planejamento import imrt as I
from tps.planejamento import vmat as V
from tps.planejamento.ajuste_mlc import verificar_mecanica
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo


def _colim(maq):
    return Colimacao.de_maquina(yaml.safe_load(open(f"dados/maquinas/{maq}/maquina.yaml", encoding="utf-8")))


def test_geometria_do_arco_elekta():
    assert V.comprimento_arco(181, 179, "CW") == pytest.approx(358)
    assert V.comprimento_arco(179, 181, "CC") == pytest.approx(358)
    assert V.comprimento_arco(30, 330, "CC") == pytest.approx(60)
    assert not V.validar_arco(181, 179, "CW") and not V.validar_arco(179, 181, "CC")
    assert V.validar_arco(170, 190, "CW")                       # passaria pelo 180: o Elekta não faz
    assert not V.validar_arco(300, 60, "CW")
    a = V.angulos_cp(181, 179, "CW", 4.0)
    assert len(a) == 91 and a[0] == pytest.approx(181) and a[-1] == pytest.approx(179)
    assert np.allclose(np.diff(np.unwrap(np.radians(a))), np.radians(358 / 90))


@pytest.mark.parametrize("maq", ["radioserra_precise", "oncomed_synergy_154828"])
def test_reparar_sempre_cumpre_a_mecanica(maq):
    col = _colim(maq)
    n = col.n_pares
    rng = np.random.default_rng(3)
    for _ in range(500):
        a = rng.uniform(-60, 40, n)
        b = a + rng.uniform(-10, 40, n)
        V.reparar(a, b, col, -70, 70, rng.random(n) < 0.6)
        c = Campo("x", 0, jaw_x=(-70, 70), jaw_y=(-100, 100), mlc_a=list(a), mlc_b=list(b))
        assert not verificar_mecanica(c, col)
        assert np.allclose(a * 10, np.round(a * 10)) and np.allclose(b * 10, np.round(b * 10))


def test_sequenciamento_do_setor_reproduz_a_fluencia_e_varre_num_sentido():
    col = _colim("oncomed_synergy_154828")                      # Agility: interdigita, gap 0
    u = np.arange(-50, 50, 2.5) + 1.25
    nv = col.n_pares
    ab = np.zeros((nv, len(u)), bool)
    ab[30:50, 4:36] = True
    yy, xx = np.mgrid[0:nv, 0:len(u)]
    F = np.where(ab, 1.0 + 0.5 * np.sin(xx / 5.0) + 0.3 * np.cos(yy / 4.0), 0.0)
    for sentido in (1, -1):
        ap, delta = V.sequenciar_setor(F, ab, u, 8, sentido, col, -60, 60)
        assert len(ap) == 8
        entregue = sum(I._cobertura(a, b, u, 2.5)[0] for a, b in ap) * delta
        assert np.abs(entregue - F)[ab].mean() < 0.6 * delta                # erro de quantização
        A = np.array([a for a, _ in ap])[:, 30:50]
        B = np.array([b for _, b in ap])[:, 30:50]
        # varredura num sentido: as duas lâminas só andam para o mesmo lado
        assert np.all(np.diff(A, axis=0) * sentido >= -1e-6) and np.all(np.diff(B, axis=0) * sentido >= -1e-6)


def test_intervalos_do_arco_e_exportacao_dynamic(tmp_path):
    from tps.exportacao.rtplan import _feixes_para_exportar
    col = _colim("radioserra_precise")
    n = col.n_pares
    pcs = []
    for k, g in enumerate([181.0, 185.0, 189.0]):
        a = np.full(n, -10.0 + 5 * k)
        pcs.append({"gantry": g, "mlc_a": a.tolist(), "mlc_b": (a + 20).tolist(), "jaw_x": [-30, 30], "jaw_y": [-40, 40],
                    "mw": [0.0, 0.25, 1.0][k]})
    c = Campo("ARCO1", 181.0, jaw_x=(-30, 30), jaw_y=(-40, 40), mlc_a=[-10.0] * n, mlc_b=[20.0] * n, mu=200.0,
              arco_fim=189.0, sentido="CW", pontos_controle=pcs)
    iv = calculo.intervalos_arco(c)
    assert [round(s["gantry"]) for s in iv] == [183, 187]
    assert [s["peso"] for s in iv] == pytest.approx([0.25, 0.75])
    assert iv[0]["mlc_a"][0] == pytest.approx(-7.5)
    segs = calculo.segmentos_do_campo(c, col)
    assert len(segs) == 2 and sum(s.mu for s in segs) == pytest.approx(1.0)
    (nome, cc, mu, formas, membros), = _feixes_para_exportar(type("P", (), {"campos": [c]})(), "campos_separados")
    assert formas is None and mu == 200.0


def test_hash_de_campo_estatico_nao_muda_com_os_campos_de_arco():
    from tps.planejamento.modelo import Plano, Prescricao
    p = Plano("X", "1", "m", "M", Prescricao(50, 25, "PTV", "media_alvo"),
              [Campo("G0", 0.0, jaw_x=(-50, 50), jaw_y=(-50, 50), mlc_a=[-50.0], mlc_b=[50.0], mu=100.0)])
    d = p.conteudo()
    assert "arco_fim" not in d["campos"][0] and "pontos_controle" not in d["campos"][0]


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_vmat")
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


def test_vmat_de_ponta_a_ponta(cli):
    c, sid, home = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "VMAT", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    assert c.post(f"/api/{sid}/vmat/arcos", json={"n": 1, "inicio": 170, "fim": 190, "sentido": "CW"}).status_code == 400
    r = _ok(c.post(f"/api/{sid}/vmat/arcos", json={"n": 1, "inicio": 181, "fim": 179, "sentido": "CW", "margem_mm": 5}))
    (arco,) = r["plano"]["campos"]
    assert arco["arco_fim"] == 179 and arco["sentido"] == "CW" and r["plano"]["tecnica"] == "VMAT"
    assert c.post(f"/api/{sid}/calcular", json={}).status_code == 400          # arco sem pontos de controle
    obj = [{"estrutura": "PTV_fantoma", "tipo": "alvo", "dose_gy": 50, "peso": 5},
           {"estrutura": "Osso_inserto", "tipo": "media", "dose_gy": 15, "peso": 1, "restrito": True}]
    r = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo", "vmat": {
        "objetivos": obj, "iteracoes": 1, "setor_graus": 32}}))
    vm = r["resultado"]["info"]["vmat"]
    assert r["plano"]["tecnica"] == "VMAT" and vm["arcos"]["ARCO1"]["pontos"] == 91
    assert 0 < vm["arcos"]["ARCO1"]["tempo_s"] < 300
    est = r["resultado"]["estatisticas"]
    assert est["PTV_fantoma"]["D95"] > 0.93 * 50
    assert est["Osso_inserto"]["Dmean"] < 15 * 1.03
    conf = r["resultado"]["conferencia_um"]
    comp = [k for k in conf if k.get("composta")]
    assert comp and abs(comp[0]["dif_pct"]) < 10
    chk = {i["id"]: i for i in _ok(c.get(f"/api/{sid}/checklist"))}
    assert chk["imrt_qa"]["estado"] == "aviso" and "VMAT" in chk["imrt_qa"]["titulo"]
    # recálculo sem a opção VMAT mantém os pontos de controle e a dose
    d95 = est["PTV_fantoma"]["D95"]
    r2 = _ok(c.post(f"/api/{sid}/calcular", json={}))
    assert r2["plano"]["campos"][0]["n_pontos"] == 91
    assert r2["resultado"]["estatisticas"]["PTV_fantoma"]["D95"] == pytest.approx(d95, abs=0.05)
    # QA (gantry 0): o RTPLAN é o arco real
    qa = _ok(c.post(f"/api/{sid}/qa_paciente", json={"modo": "gantry0", "fantoma": "cubo30"}))
    rp = pydicom.dcmread(qa["arquivos"][0])
    assert rp.BeamSequence[0].BeamType == "DYNAMIC" and len(rp.BeamSequence[0].ControlPointSequence) == 91
    # exportação: feixe DYNAMIC, gantry andando no sentido CW, UM acumulada crescente
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1", "justificativa": "Teste automático do VMAT"}))
    ex = _ok(c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1", "rtstruct": False}))
    rp = pydicom.dcmread(next(a for a in ex["arquivos"] if "RP_" in a))
    (b,) = rp.BeamSequence
    cps = b.ControlPointSequence
    assert b.BeamType == "DYNAMIC" and cps[0].GantryRotationDirection == "CW" and cps[-1].GantryRotationDirection == "NONE"
    assert float(cps[0].GantryAngle) == pytest.approx(181) and float(cps[-1].GantryAngle) == pytest.approx(179)
    w = [float(cp.CumulativeMetersetWeight) for cp in cps]
    assert w[0] == 0 and w[-1] == 1 and w == sorted(w)
    assert all(len(cp.BeamLimitingDevicePositionSequence) == 3 for cp in cps)        # ASYMX, ASYMY, MLCX
