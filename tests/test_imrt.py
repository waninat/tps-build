"""IMRT step-and-shoot: sequenciamento (sweep), mecânica do MLCi, otimização inversa de ponta a ponta no fantoma,
exportação e plano de verificação (QA de paciente). SRS-IMR / SRS-QA."""
import numpy as np
import pydicom
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths
from tps.planejamento import imrt as I
from tps.planejamento.ajuste_mlc import verificar_mecanica
from tps.planejamento.conformacao import Colimacao
from tps.planejamento.modelo import Campo
from tps.planejamento.plano_2d import CampoRetangular, campo_de_retangulo


def test_sweep_entrega_o_perfil_exato():
    rng = np.random.default_rng(3)
    for _ in range(50):
        p = rng.integers(0, 6, 30)
        p[:3] = 0
        f = np.zeros(32, int)
        for a, b in I.intervalos_sweep(p):
            assert a < b
            f[a:b] += 1
        assert np.array_equal(f[:30], p)
        assert len(I.intervalos_sweep(p)) == int(np.maximum(np.diff(np.r_[0, p]), 0).sum())


def test_sequenciamento_respeita_a_mecanica_do_mlci():
    colim = Colimacao.mlci()
    c = campo_de_retangulo(CampoRetangular(nome="T", gantry=0, x1_mm=60, x2_mm=60, y1_mm=50, y2_mm=50), colim,
                           (0, 0, 0), 6)
    u, v, ab = I._grade_fluencia(c, colim, 2.5)
    from scipy.ndimage import gaussian_filter
    rng = np.random.default_rng(1)
    F = gaussian_filter(rng.random(ab.shape), (0.7, 2.5))          # fluência suave, como a otimizada
    Fq = np.where(ab, np.rint((F - F[ab].min()) / np.ptp(F[ab]) * 6), 0).astype(int)
    segs = I.sequenciar_sweep(c, colim, u, Fq, ab)
    assert segs
    entregue = np.zeros(ab.shape)
    for s in segs:
        sc = Campo("s", 0, jaw_x=s["jaw_x"], jaw_y=s["jaw_y"], mlc_a=s["mlc_a"], mlc_b=s["mlc_b"])
        assert not verificar_mecanica(sc, colim)
        for i in range(ab.shape[0]):
            if s["jaw_y"][0] < v[i] < s["jaw_y"][1]:
                entregue[i] += s["peso"] * ((u > s["mlc_a"][i]) & (u < s["mlc_b"][i]))
    # a entrega bate com a fluência, a menos do gap mínimo de 5 mm (intervalos de 1 coluna e pares fechados)
    dif = np.abs(entregue[ab] - Fq[ab])
    assert dif.mean() < 0.35 and np.percentile(dif, 90) <= 1


def test_objetivos_sugeridos_vem_do_protocolo():
    obj = I.objetivos_sugeridos(["PTV", "RETO", "BEXIGA", "BODY"], "PTV", 70.0)
    assert obj[0] == {"estrutura": "PTV", "tipo": "alvo", "dose_gy": 70.0, "volume_pct": None, "peso": 5.0}
    tipos = {(o["estrutura"], o["tipo"]) for o in obj}
    assert ("RETO", "dvh") in tipos and ("BEXIGA", "dvh") in tipos


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_imrt")
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


def test_imrt_de_ponta_a_ponta(cli):
    c, sid, home = cli
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "IMRT", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    r = _ok(c.post(f"/api/{sid}/imrt/campos", json={"n": 5, "inicio": 0, "margem_mm": 5}))
    assert [round(q["gantry"]) for q in r["plano"]["campos"]] == [0, 72, 144, 216, 288]
    r0 = _ok(c.post(f"/api/{sid}/calcular", json={}))
    osso0 = r0["resultado"]["estatisticas"]["Osso_inserto"]["Dmean"]
    sug = _ok(c.get(f"/api/{sid}/imrt/objetivos"))
    assert sug[0]["tipo"] == "alvo"
    obj = [{"estrutura": "PTV_fantoma", "tipo": "alvo", "dose_gy": 50, "peso": 5},
           {"estrutura": "Osso_inserto", "tipo": "media", "dose_gy": 0.6 * osso0, "peso": 2}]
    r = _ok(c.post(f"/api/{sid}/calcular", json={"metodo": "media_alvo", "imrt": {
        "objetivos": obj, "niveis": 5, "iteracoes": 1, "um_min": 2, "area_min_cm2": 2}}))
    im = r["resultado"]["info"]["imrt"]
    assert sum(im["segmentos"].values()) >= len(im["segmentos"]) and r["plano"]["tecnica"] == "IMRT"
    est = r["resultado"]["estatisticas"]
    assert est["Osso_inserto"]["Dmean"] < osso0                         # o órgão protegido recebe menos
    assert est["PTV_fantoma"]["D95"] > 0.9 * 50
    conf = r["resultado"]["conferencia_um"]
    comp = [k for k in conf if k.get("composta")]
    assert comp and abs(comp[0]["dif_pct"]) < 10
    chk = {i["id"]: i for i in _ok(c.get(f"/api/{sid}/checklist"))}
    assert chk["imrt_qa"]["estado"] == "aviso"
    # recálculo sem a opção IMRT mantém os segmentos
    r2 = _ok(c.post(f"/api/{sid}/calcular", json={}))
    assert r2["plano"]["tecnica"] == "IMRT"
    assert sum(len(q["segmentos"] or []) for q in r2["plano"]["campos"]) == sum(im["segmentos"].values())
    # QA de paciente
    qa = _ok(c.post(f"/api/{sid}/qa_paciente", json={"modo": "gantry0", "fantoma": "cubo30"}))
    assert qa["dose_iso_gy"] > 0 and len(qa["arquivos"]) == 3
    rp = pydicom.dcmread(qa["arquivos"][0])
    assert all(float(b.ControlPointSequence[0].GantryAngle) == 0 for b in rp.BeamSequence)
    assert sum(len(b.ControlPointSequence) for b in rp.BeamSequence) == 2 * sum(im["segmentos"].values())
    rd = pydicom.dcmread(qa["arquivos"][1])
    assert rd.Modality == "RTDOSE"
    # exportação do plano do paciente: um feixe STATIC por campo com os segmentos como pares de pontos de controle
    _ok(c.post(f"/api/{sid}/aprovar", json={"senha": "senha-forte-1",
                                            "justificativa": "Teste automático do IMRT no fantoma"}))
    ex = _ok(c.post(f"/api/{sid}/exportar", json={"senha": "senha-forte-1", "rtstruct": False}))
    rp = pydicom.dcmread(next(a for a in ex["arquivos"] if "RP_" in a))
    assert len(rp.BeamSequence) == len(im["segmentos"])
    for b in rp.BeamSequence:
        pesos = [float(cp.CumulativeMetersetWeight) for cp in b.ControlPointSequence]
        assert pesos[0] == 0 and abs(pesos[-1] - 1) < 1e-6 and pesos == sorted(pesos)
