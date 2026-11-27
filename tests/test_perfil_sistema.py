"""Perfil de planejamento (Monaco / Eclipse): vocabulário, padrões e ficha de objetivos. SRS-PLN-120."""
import xml.etree.ElementTree as ET

import numpy as np
import pytest

from tps.planejamento import imrt as I
from tps.planejamento import perfil_sistema as PS


def test_eclipse_para_interno():
    up0 = PS.para_interno("eclipse", {"estrutura": "MEDULA", "funcao": "Upper", "dose_gy": 45, "volume_pct": 0,
                                      "prioridade": 400})
    assert up0["tipo"] == "max" and up0["peso"] == 4.0 and up0["restrito"]
    up = PS.para_interno("eclipse", {"estrutura": "RETO", "funcao": "Upper", "dose_gy": 50, "volume_pct": 35,
                                     "prioridade": 80})
    assert up["tipo"] == "dvh" and up["volume_pct"] == 35 and not up["restrito"]
    lo = PS.para_interno("eclipse", {"estrutura": "PTV", "funcao": "Lower", "dose_gy": 50, "volume_pct": 100})
    assert lo["tipo"] == "min"
    lo95 = PS.para_interno("eclipse", {"estrutura": "PTV", "funcao": "Lower", "dose_gy": 50, "volume_pct": 95})
    assert lo95["tipo"] == "dvh_min" and lo95["volume_pct"] == 95
    g = PS.para_interno("eclipse", {"estrutura": "TRONCO", "funcao": "Upper gEUD", "dose_gy": 40, "k": 12})
    assert g["tipo"] == "serial" and g["volume_pct"] == 12
    with pytest.raises(ValueError):
        PS.para_interno("eclipse", {"estrutura": "X", "funcao": "Target Penalty", "dose_gy": 1})


def test_monaco_ida_e_volta():
    for o in ({"estrutura": "R", "tipo": "dvh", "dose_gy": 50, "volume_pct": 30, "peso": 1},
              {"estrutura": "M", "tipo": "serial", "dose_gy": 40, "volume_pct": 12, "peso": 1},
              {"estrutura": "B", "tipo": "paralelo", "dose_gy": 30, "volume_pct": 40, "peso": 1}):
        r = PS.de_interno("monaco", o)[0]
        de_volta = PS.para_interno("monaco", r)
        assert de_volta["tipo"] == o["tipo"] and de_volta["dose_gy"] == o["dose_gy"]


def test_dvh_min_gradiente():
    rng = np.random.default_rng(1)
    d = rng.uniform(40, 52, 400)
    o = I.Objetivo("PTV", None, "dvh_min", 50.0, 95.0)
    f, g = I._termo(o, d)
    assert f > 0
    eps = 1e-5
    k = int(np.argsort(d)[::-1][100])
    d2 = d.copy()
    d2[k] += eps
    assert (I._termo(o, d2)[0] - f) / eps == pytest.approx(g[k], rel=1e-3, abs=1e-9)
    assert I._termo(o, np.full(100, 51.0))[0] == 0


def test_ficha_eclipse(tmp_path):
    objs = [{"estrutura": "PTV", "tipo": "alvo", "dose_gy": 50}, {"estrutura": "RETO", "tipo": "dvh", "dose_gy": 50,
            "volume_pct": 35, "peso": 1}, {"estrutura": "MEDULA", "tipo": "serial", "dose_gy": 40, "volume_pct": 12}]
    arqs = PS.gravar_ficha("eclipse", objs, tmp_path, "PL1", "PL1 — 50 Gy")
    nomes = sorted(a.name for a in arqs)
    assert nomes == ["OBJ_Eclipse_PL1.csv", "OBJ_Eclipse_PL1.xml"]
    raiz = ET.parse(tmp_path / "OBJ_Eclipse_PL1.xml").getroot()
    ests = {e.get("ID"): e for e in raiz.iter("ObjectivesOneStructure")}
    assert set(ests) == {"PTV", "RETO", "MEDULA"}
    ptv = [o.attrib for o in ests["PTV"].iter("Objective")]
    assert {o["Operator"] for o in ptv} == {"Lower", "Upper"}
    assert any(o.get("ParameterA") == "12" for o in ests["MEDULA"].iter("Objective") for o in [o.attrib])
    assert raiz.find("Helpers/NormalTissueObjective").get("Priority") == "100"
    assert [a.name for a in PS.gravar_ficha("monaco", objs, tmp_path, "PL1")] == ["OBJ_Monaco_PL1.csv"]


def test_padroes_e_pedido():
    o = PS.opcoes_otimizacao("eclipse", {"niveis": 6, "um_min": None})
    assert o["niveis"] == 6 and o["um_min"] == 3.0
    assert PS.convencoes_exportacao("eclipse")["rotulo_max"] == 13
