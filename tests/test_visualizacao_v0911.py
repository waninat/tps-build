"""0.9.11: feixes nos 3 cortes, malha 3D, meio campo (junção mono-isocêntrica), BODY sem ilhas finas, sítio pelo
nome do exame, preferência de GPU/CPU da segmentação, ferramentas do visualizador (ponto, info, isocentro,
referência, renomear). SRS-UI/AUT."""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from tps import fantoma, paths

SENHA = "senha-forte-1"


@pytest.fixture(scope="module")
def cli(tmp_path_factory):
    home = tmp_path_factory.mktemp("home_v0911")
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
    sid = c.post("/api/caso/ct", json={"pasta": str(caso)}).json()["id"]
    c.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"})
    _ok(c.post(f"/api/{sid}/plano", json={"rotulo": "V0911", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"}))
    for g in (0, 90):
        _ok(c.post(f"/api/{sid}/campo", json={"gantry": g, "modo": "conformado", "alvo": "PTV_fantoma",
                                              "margem": {"x1": 7, "x2": 7, "y1": 7, "y2": 7}}))
    return c, sid


def _ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def test_feixes_nos_tres_cortes(cli):
    c, sid = cli
    r = _ok(c.get(f"/api/{sid}"))
    nz, ny, nx = r["ct"]["z"][2], r["ct"]["y"][2], r["ct"]["x"][2]
    ax = _ok(c.get(f"/api/{sid}/sobreposicao/axial/{nz // 2}"))
    co = _ok(c.get(f"/api/{sid}/sobreposicao/coronal/{ny // 2}"))
    sa = _ok(c.get(f"/api/{sid}/sobreposicao/sagital/{nx // 2}"))
    f_ax = {f["nome"]: f for f in ax["feixes"]}
    assert set(f_ax) == {"G000", "G090"} and all(len(f["bordas"]) == 4 for f in f_ax.values())
    assert all("entrada" in f for f in f_ax.values())          # entrada do eixo central na pele
    f_co = {f["nome"]: f for f in co["feixes"]}
    assert "secao" in f_co["G000"] and len(f_co["G000"]["secao"]) >= 4   # o AP cruza o plano coronal: secção
    assert f_co["G090"]["bordas"] and "secao" not in f_co["G090"]
    f_sa = {f["nome"]: f for f in sa["feixes"]}
    assert "secao" in f_sa["G090"]                               # o lateral cruza o sagital


def test_bev_tem_quadro_e_cena_3d(cli):
    c, sid = cli
    b = _ok(c.get(f"/api/{sid}/bev/G090"))
    q = b["quadro"]
    assert np.allclose(np.cross(q["u"], q["v"]), q["w"], atol=1e-6) or np.allclose(np.cross(q["u"], q["v"]), -np.array(q["w"]), atol=1e-6)
    cena = _ok(c.get(f"/api/{sid}/cena3d"))
    assert len(cena["campos"]) == 2 and len(cena["campos"][0]["abertura"]) >= 4
    m = _ok(c.get(f"/api/{sid}/malha/PTV_fantoma?passo=3"))
    v = np.array(m["v"]).reshape(-1, 3)
    f = np.array(m["f"]).reshape(-1, 3)
    assert len(f) > 50 and f.max() < len(v)
    info = _ok(c.get(f"/api/{sid}/estrutura_info/PTV_fantoma"))
    lo, hi = np.array(info["min_mm"]), np.array(info["max_mm"])
    assert np.all(v.min(axis=0) > lo - 6) and np.all(v.max(axis=0) < hi + 6)   # a malha fica em volta da estrutura


def test_malha_esfera_orientada():
    from tps.app.malha3d import malha
    x = np.arange(-60, 60, 2.0)
    z = np.arange(-45, 45, 3.0)
    Z, Y, X = np.meshgrid(z, x, x, indexing="ij")
    m = X ** 2 + Y ** 2 + Z ** 2 < 30 ** 2
    r = malha(m, (x, x, z), passo_mm=3.0)
    V = np.array(r["v"]).reshape(-1, 3)
    F = np.array(r["f"]).reshape(-1, 3)
    assert abs(np.linalg.norm(V, axis=1).mean() - 30) < 2.5
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    assert (np.einsum("ij,ij->i", n, V[F].mean(axis=1)) > 0).mean() > 0.99   # normais para fora


def test_meio_campo_fecha_jaw_e_laminas(cli):
    c, sid = cli
    r = _ok(c.post(f"/api/{sid}/campo", json={"nome": "MEIO", "gantry": 0, "modo": "conformado", "alvo": "PTV_fantoma",
                                              "margem": {"x1": 7, "x2": 7, "y1": 7, "y2": 7}, "fechar_y": "y2"}))
    cp = next(x for x in r["plano"]["campos"] if x["nome"] == "MEIO")
    assert cp["jaw_y"][1] == 0.0 and cp["jaw_y"][0] < 0
    mq = _ok(c.get(f"/api/{sid}"))["maquina"]
    lim, gap = mq["limites"], mq["gap"] or 0.0
    for i in range(len(lim) - 1):
        if lim[i] >= 0:
            assert cp["mlc_b"][i] - cp["mlc_a"][i] <= gap + 0.5       # pares do lado fechado: fechados
    _ok(c.delete(f"/api/{sid}/campo/MEIO"))


def test_ferramentas_do_visualizador(cli):
    c, sid = cli
    p = _ok(c.get(f"/api/{sid}/ponto?x=0&y=0&z=0"))
    assert p["hu"] is not None and p["densidade"] > 0.5
    info = _ok(c.get(f"/api/{sid}/estrutura_info/PTV_fantoma"))
    assert info["volume_cc"] > 10 and info["hu_medio"] is not None and "densidade_media" in info
    r = _ok(c.post(f"/api/{sid}/isocentro", json={"estrutura": "PTV_fantoma", "arredondar_mm": 5}))
    assert all(abs(v / 5 - round(v / 5)) < 1e-6 for v in r["isocentro"])
    ref = _ok(c.post(f"/api/{sid}/referencia", json={"xyz": [0, 0, 0]}))
    assert ref["resumo"]["plano"]["referencia_mm"] == [0, 0, 0]
    d = ref["resumo"]["deslocamentos"]
    assert d and {"lateral", "vertical", "longitudinal"} <= set(d)
    r = _ok(c.post(f"/api/{sid}/plano/renomear", json={"rotulo": "NOVO_NOME"}))
    assert r["plano"]["rotulo"] == "NOVO_NOME"


def test_body_sem_ilhas_finas():
    from tps.planejamento.contorno import corpo_3d
    hu = np.full((12, 120, 120), -1000.0)
    yy, xx = np.mgrid[:120, :120]
    perna = (xx - 60) ** 2 + (yy - 50) ** 2 < 30 ** 2
    hu[:, perna] = 20
    hu[:6, 82:86, 20:100] = -100                # colchão encostado na perna nos primeiros cortes
    hu[6:, 92:96, 20:100] = -100                # e solto (ilha fina) nos outros
    m = corpo_3d(hu, -350, (3.0, 1.0, 1.0))
    assert not m[8, 92:96, :].any()             # a ilha fina saiu
    assert m[8, 50, 60] and m[8].sum() > 0.9 * perna.sum()


def test_sitio_pelo_nome_do_exame():
    from tps.app.automacao import sitio_do_texto as S
    assert S("TC CP") == "cabeca_pescoco" and S("C.P.") == "cabeca_pescoco"
    assert S("TC CRANIO") == "encefalo" and S("SNC") == "encefalo"
    assert S("PELVE") == "pelve" and S("MAMA E") == "mama" and S("MD") == "mama"
    assert S("TORAX") == "torax" and S("MEDIASTINO") == "torax"


def test_preferencia_dispositivo_segmentacao(cli):
    c, _ = cli
    from tps.ia import segmentacao
    assert segmentacao.dispositivo_preferido() == "auto"
    assert _ok(c.post("/api/ia/dispositivo", json={"dispositivo": "cpu"}))["dispositivo"] == "cpu"
    assert segmentacao.dispositivo_preferido() == "cpu"
    assert c.post("/api/ia/dispositivo", json={"dispositivo": "tpu"}).status_code == 400
    _ok(c.post("/api/ia/dispositivo", json={"dispositivo": "auto"}))


def test_variantes_e_rotulos():
    from tps.app.automacao import _rotulo, _variantes
    pc = {"variantes": ["3D", "VMAT"], "tecnica_por_sitio": {"mama": "3D"}, "tecnica_padrao": "VMAT"}
    assert _variantes(pc, "reto") == ["3D", "VMAT"]
    assert _variantes({**pc, "variantes": []}, "mama") == ["3D"]
    assert _rotulo("PTVPelve45@25", "3D", 2) == "PTVPelve45@25_3D" and len(_rotulo("PTVPelveLonga45@25", "VMAT", 2)) <= 16
    assert _rotulo("PTVPelve45@25", "VMAT", 1) == "PTVPelve45@25"
