"""Conferência de entregabilidade (regras Elekta) e ficha de objetivos para o Monaco. SRS-EXP-030/031."""
import numpy as np
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from tps.exportacao.entregabilidade import Regras, conferir
from tps.exportacao.objetivos_monaco import gravar, traduzir


def _plano(formas, mu=100.0):
    """formas: [(jx, jy, a, b, peso_acumulado_fim)] → RTPLAN mínimo step-and-shoot MLCi (40 pares de 10 mm)."""
    ds = Dataset()
    lim = list(np.linspace(-200, 200, 41))
    b = Dataset()
    b.BeamNumber, b.BeamName, b.BeamType, b.TreatmentDeliveryType = 1, "G0", "STATIC", "TREATMENT"
    mlc = Dataset()
    mlc.RTBeamLimitingDeviceType, mlc.LeafPositionBoundaries = "MLCX", lim
    b.BeamLimitingDeviceSequence = Sequence([mlc])
    cps, acum = [], 0.0
    for jx, jy, a, bb, fim in formas:
        for w in (acum, fim):
            cp = Dataset()
            cp.CumulativeMetersetWeight = w
            pos = []
            for t, v in (("ASYMX", jx), ("ASYMY", jy), ("MLCX", list(a) + list(bb))):
                d = Dataset()
                d.RTBeamLimitingDeviceType, d.LeafJawPositions = t, v
                pos.append(d)
            cp.BeamLimitingDevicePositionSequence = Sequence(pos)
            cps.append(cp)
        acum = fim
    b.ControlPointSequence = Sequence(cps)
    ds.BeamSequence = Sequence([b])
    rb = Dataset()
    rb.ReferencedBeamNumber, rb.BeamMeterset = 1, mu
    fg = Dataset()
    fg.ReferencedBeamSequence = Sequence([rb])
    ds.FractionGroupSequence = Sequence([fg])
    return ds


def _forma(abertura):
    a, b = np.full(40, -2.5), np.full(40, 2.5)
    for i, (x0, x1) in abertura.items():
        a[i], b[i] = x0, x1
    return a, b


def test_plano_bom_passa_e_os_erros_aparecem():
    R = Regras(gap_min_mm=5, interdigitacao=False, um_min_segmento=3, area_min_cm2=2, largura_min_mm=10)
    a, b = _forma({i: (-30, 30) for i in range(17, 23)})
    assert [p for p in conferir(_plano([([-32, 32], [-30, 30], a, b, 1.0)]), R) if p["gravidade"] != "info"] == []
    # gap menor que o mínimo, interdigitação, UM baixa, fenda estreita
    a2, b2 = _forma({18: (-30, -27), 19: (10, 40), 20: (-40, 0)})
    msgs = " | ".join(p["msg"] for p in conferir(_plano([([-32, 32], [-30, 30], a, b, 0.98),
                                                          ([-42, 42], [-30, 30], a2, b2, 1.0)]), R))
    assert "gap A–B" in msgs and "interdigitação" in msgs and "2.0 UM" in msgs


def test_ficha_do_monaco(tmp_path):
    obj = [{"estrutura": "PTV", "tipo": "alvo", "dose_gy": 50}, {"estrutura": "Reto", "tipo": "dvh", "dose_gy": 40, "volume_pct": 30},
           {"estrutura": "Medula", "tipo": "max", "dose_gy": 45, "restrito": True},
           {"estrutura": "Bexiga", "tipo": "media", "dose_gy": 30}]
    t = traduzir(obj)
    fun = [(x["estrutura"], x["funcao"]) for x in t]
    assert fun == [("PTV", "Target Penalty"), ("PTV", "Quadratic Overdose"), ("Reto", "Overdose DVH"),
                   ("Medula", "Maximum Dose"), ("Bexiga", "Serial")]
    f = gravar(obj, tmp_path / "obj.csv", "teste")
    assert "Overdose DVH;Dose 40 Gy; Maximum Volume 30%" in f.read_text(encoding="utf-8-sig").replace('"', "")
