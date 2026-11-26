"""Conferência de entregabilidade de um RTPLAN nas regras mecânicas da Elekta (MLCi/MLCi2/Agility). SRS-EXP-030.

Roda no plano exportado (Dataset) — serve para os planos do TPS e para os do Monaco. Cada problema vem com o feixe,
o ponto de controle e o par de lâminas, em português. As regras seguem os limites da máquina (maquina.yaml):
gap mínimo entre as lâminas do par e entre vizinhos opostos (MLCi não interdigita), excursão máxima, diafragma X
acompanhando as lâminas abertas (MLCi), pares fechados escondidos sob as mandíbulas, UM mínima e área mínima do
segmento, e a sequência de pesos.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Regras:
    gap_min_mm: float = 5.0
    interdigitacao: bool = False
    overtravel_mm: float = 125.0
    campo_max_mm: float = 200.0
    tem_jaw_x: bool = True
    um_min_segmento: float = 2.0
    area_min_cm2: float = 2.0
    largura_min_mm: float = 5.0
    folga_diafragma_mm: float = 0.0      # o diafragma X não pode ficar para dentro de lâmina aberta
    um_por_grau_min: float = 0.1         # arcos (Elekta): UM por grau entre o mínimo e o máximo da máquina
    um_por_grau_max: float = 20.0

    @classmethod
    def da_maquina(cls, colim, raw: dict | None = None) -> "Regras":
        imrt = ((raw or {}).get("imrt") or {})
        vm = ((raw or {}).get("vmat") or {})
        mlc = (((raw or {}).get("collimation") or {}).get("mlc") or {})
        return cls(gap_min_mm=float(colim.gap_minimo_mm), interdigitacao=bool(mlc.get("interdigitation", True)),
                   overtravel_mm=float(getattr(colim, "overtravel_mm", 125.0) or 125.0), tem_jaw_x=colim.tem_jaw_x,
                   um_min_segmento=float(imrt.get("um_min_segmento", 2.0)),
                   area_min_cm2=float(imrt.get("area_min_cm2", 2.0)),
                   largura_min_mm=float(getattr(colim, "largura_min_segmento_mm", 0.0) or max(5.0, colim.gap_minimo_mm)),
                   um_por_grau_min=float(vm.get("um_por_grau_min", 0.1)), um_por_grau_max=float(vm.get("um_por_grau_max", 20.0)))


def _pos(cp, tipo):
    for d in getattr(cp, "BeamLimitingDevicePositionSequence", []) or []:
        if d.RTBeamLimitingDeviceType in tipo:
            return [float(v) for v in d.LeafJawPositions]
    return None


def conferir(ds, regras: Regras) -> list[dict]:
    """[{gravidade: 'erro'|'aviso', feixe, cp, msg}]"""
    out = []
    mus = {int(rb.ReferencedBeamNumber): float(getattr(rb, "BeamMeterset", 0) or 0)
           for fg in ds.FractionGroupSequence for rb in fg.ReferencedBeamSequence}
    g, tol = regras.gap_min_mm, 0.05
    for b in ds.BeamSequence:
        if getattr(b, "TreatmentDeliveryType", "TREATMENT") != "TREATMENT":
            continue
        nome = str(b.BeamName)
        limites = None
        for d in b.BeamLimitingDeviceSequence:
            if d.RTBeamLimitingDeviceType.startswith("MLC"):
                limites = np.asarray([float(v) for v in d.LeafPositionBoundaries])
        cps = list(b.ControlPointSequence)
        jx = _pos(cps[0], ("ASYMX", "X")) or [-200.0, 200.0]
        jy = _pos(cps[0], ("ASYMY", "Y")) or [-200.0, 200.0]
        mlc = _pos(cps[0], ("MLCX",))
        pesos = [float(cp.CumulativeMetersetWeight) for cp in cps]
        if any(p2 < p1 - 1e-9 for p1, p2 in zip(pesos, pesos[1:])) or abs(pesos[-1] - 1) > 1e-6 or pesos[0] != 0:
            out.append({"gravidade": "erro", "feixe": nome, "cp": None, "msg": "pesos acumulados fora de ordem"})
        mu = mus.get(int(b.BeamNumber), 0.0)
        dinamico = str(b.BeamType) == "DYNAMIC"
        if dinamico and len(cps) > 1:
            gs = [float(getattr(cp, "GantryAngle", 0) or 0) for cp in cps]
            for k in range(1, len(cps)):
                dg = abs((gs[k] - gs[k - 1] + 180.0) % 360.0 - 180.0)
                if dg < 1e-6:
                    continue
                upg = mu * (pesos[k] - pesos[k - 1]) / dg
                if upg < regras.um_por_grau_min - 1e-6 or upg > regras.um_por_grau_max + 1e-6:
                    out.append({"gravidade": "erro", "feixe": nome, "cp": k,
                                "msg": f"{upg:.3f} UM/grau fora da faixa da máquina "
                                       f"({regras.um_por_grau_min:g}–{regras.um_por_grau_max:g})"})
                    break
        for k, cp in enumerate(cps):
            jx = _pos(cp, ("ASYMX", "X")) or jx
            jy = _pos(cp, ("ASYMY", "Y")) or jy
            mlc = _pos(cp, ("MLCX",)) or mlc
            if mlc is None or limites is None:
                continue
            n = len(mlc) // 2
            a, bb = np.asarray(mlc[:n]), np.asarray(mlc[n:])
            ctx = {"feixe": nome, "cp": k}
            if jy[1] - jy[0] < 5 or (regras.tem_jaw_x and jx[1] - jx[0] < 5):
                out.append({**ctx, "gravidade": "erro", "msg": f"mandíbulas quase fechadas (X {jx}, Y {jy})"})
            if np.any(a > regras.overtravel_mm + tol) or np.any(bb < -regras.overtravel_mm - tol):
                i = int(np.argmax((a > regras.overtravel_mm + tol) | (bb < -regras.overtravel_mm - tol)))
                out.append({**ctx, "gravidade": "erro", "msg": f"par {i + 1}: lâmina além da excursão máxima "
                                                              f"({regras.overtravel_mm:g} mm)"})
            if np.any(np.abs(np.r_[a, bb]) > regras.campo_max_mm + tol):
                out.append({**ctx, "gravidade": "erro", "msg": "lâmina fora do campo máximo"})
            gap = bb - a
            ruins = np.nonzero(gap < g - tol)[0]
            if ruins.size:
                out.append({**ctx, "gravidade": "erro", "msg": f"gap A–B menor que {g:g} mm nos pares "
                                                              f"{', '.join(str(i + 1) for i in ruins[:8])}"
                                                              f"{'…' if ruins.size > 8 else ''} (mín {gap.min():.1f} mm)"})
            if not regras.interdigitacao:
                viz = np.r_[bb[1:] - a[:-1], bb[:-1] - a[1:]]
                if np.any(viz < g - tol):
                    j = int(np.argmin(viz))
                    out.append({**ctx, "gravidade": "erro", "msg": f"interdigitação (MLCi): lâmina A passa a B do par "
                                                                  f"vizinho (folga {viz[j]:.1f} mm < {g:g})"})
            # pares dentro da abertura Y
            dentro = (limites[1:] > jy[0] + 0.5) & (limites[:-1] < jy[1] - 0.5)
            aberto = dentro & (gap > g + 0.5)
            fechado = dentro & ~aberto
            if regras.tem_jaw_x and aberto.any():
                if jx[0] > a[aberto].min() + 0.5 + regras.folga_diafragma_mm or \
                        jx[1] < bb[aberto].max() - 0.5 - regras.folga_diafragma_mm:
                    out.append({**ctx, "gravidade": "aviso",
                                "msg": f"diafragma X ({jx[0]:.1f}, {jx[1]:.1f}) para dentro das lâminas abertas "
                                       f"({a[aberto].min():.1f}, {bb[aberto].max():.1f})"})
            if fechado.any():
                centro = (a[fechado] + bb[fechado]) / 2
                expostos = (centro > jx[0] + 1) & (centro < jx[1] - 1) if regras.tem_jaw_x else np.ones(centro.size, bool)
                if np.any(expostos):
                    out.append({**ctx, "gravidade": "info",
                                "msg": f"{int(np.sum(expostos))} par(es) fechado(s) dentro da abertura das mandíbulas "
                                       f"(vazamento pelo gap, já incluído no cálculo)"})
            if not aberto.any() and k % 2 == 1 and not dinamico:
                out.append({**ctx, "gravidade": "erro", "msg": "segmento sem abertura"})
            # segmento (step-and-shoot: pares de pontos de controle com a mesma forma)
            if not dinamico and k % 2 == 1:
                w = (pesos[k] - pesos[k - 1]) * mu
                if w < regras.um_min_segmento - 0.05:
                    out.append({**ctx, "gravidade": "erro", "msg": f"segmento com {w:.1f} UM "
                                                                  f"(mínimo {regras.um_min_segmento:g})"})
                lx = np.clip(np.minimum(bb, jx[1]) - np.maximum(a, jx[0]), 0, None)
                ly = np.clip(np.minimum(limites[1:], jy[1]) - np.maximum(limites[:-1], jy[0]), 0, None)
                area = float(np.sum(lx * ly) / 100.0)
                if area < regras.area_min_cm2:
                    out.append({**ctx, "gravidade": "aviso", "msg": f"segmento pequeno: {area:.1f} cm² "
                                                                    f"(mínimo {regras.area_min_cm2:g})"})
                larg = gap[aberto]
                if larg.size and larg.min() < regras.largura_min_mm - tol:
                    out.append({**ctx, "gravidade": "aviso", "msg": f"fenda estreita: par aberto com {larg.min():.1f} mm "
                                                                    f"(mínimo {regras.largura_min_mm:g} mm)"})
    return out
