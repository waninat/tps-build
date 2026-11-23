"""Modelos (templates) de plano por sítio e ângulos automáticos de tangentes de mama. SRS-PLN-080..086.

Cada modelo é um YAML em dados/modelos_plano/ (os da clínica, em <home>/modelos_plano/, têm prioridade e podem
acrescentar novos). O modelo diz: dose/frações sugeridas, como achar o alvo (padrões de nome), campos (ângulos,
margens, ponto de encontro, blindagens por nome TG-263), normalização, field-in-field e proteção de OARs.
O plano gerado é um rascunho comum: tudo pode ser mudado depois.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import yaml

from tps import paths
from tps.planejamento.conformacao import projetar_bev
from tps.planejamento.modelo import Campo
from tps.planejamento.restricoes import nome_tg263

PADRAO = Path(__file__).resolve().parents[2] / "dados" / "modelos_plano"


def carregar() -> dict[str, dict]:
    out = {}
    for pasta in (PADRAO, paths.home() / "modelos_plano"):
        if not pasta.is_dir():
            continue
        for f in sorted(pasta.glob("*.yaml")):
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if d.get("id"):
                d["_arquivo"] = str(f)
                out[d["id"]] = d
    return out


def lista() -> list[dict]:
    return [{"id": k, "nome": v.get("nome", k), "sitio": v.get("sitio", ""), "descricao": v.get("descricao", ""),
             "dose_total": v.get("dose_total"), "fracoes": v.get("fracoes")} for k, v in carregar().items()]


def achar_estrutura(nomes: list[str], padroes: list[str]) -> str | None:
    """Primeira estrutura cujo nome casa (regex, sem maiúsculas) ou cujo TG-263 é o pedido."""
    for p in padroes or []:
        for n in nomes:
            if p.startswith("tg:"):
                if nome_tg263(n) == p[3:]:
                    return n
            elif re.search(p, n, re.IGNORECASE):
                return n
    return None


def achar_todas(nomes: list[str], padroes: list[str]) -> list[str]:
    out = []
    for p in padroes or []:
        for n in nomes:
            ok = nome_tg263(n) == p[3:] if p.startswith("tg:") else bool(re.search(p, n, re.IGNORECASE))
            if ok and n not in out:
                out.append(n)
    return out


def lateralidade(alvo: np.ndarray, corpo: np.ndarray, x: np.ndarray) -> str:
    """'E' ou 'D' (paciente em HFS: esquerda = +x)."""
    xa = x[np.nonzero(alvo)[2]].mean()
    xc = x[np.nonzero(corpo)[2]].mean()
    return "E" if xa > xc else "D"


def tangentes(alvo: np.ndarray, pulmao: np.ndarray, eixos: tuple, iso, lado: str, margem_mm: float = 7.0,
              coracao: np.ndarray | None = None, passo: float = 2.0, sad_mm: float = 1000.0) -> dict:
    """Ângulos das tangentes de mama que minimizam o pulmão (e o coração, se houver) dentro do campo, com a borda
    posterior dos dois campos no mesmo plano (sem divergência para o pulmão), como no Eclipse.

    Mama E (HFS): medial ≈ 290–330°, lateral = medial − 180° + θ. Mama D: medial ≈ 30–70°, lateral = medial + 180° − θ.
    θ = 2·atan(d/SAD), d = distância do eixo à borda posterior no plano do isocentro."""
    x, y, z = eixos
    rng = np.random.default_rng(0)

    def pts(m, n):
        k = np.flatnonzero(m)
        if len(k) > n:
            k = rng.choice(k, n, replace=False)
        kz, ky, kx = np.unravel_index(k, m.shape)
        return np.column_stack([x[kx], y[ky], z[kz]])

    pa, pl = pts(alvo, 15000), pts(pulmao, 30000)
    pc = pts(coracao, 10000) if coracao is not None and coracao.any() else np.zeros((0, 3))
    iso = tuple(float(v) for v in iso)
    faixa = np.arange(290, 332, passo) if lado == "E" else np.arange(30, 72, passo)

    def no_campo(g, p):
        c = Campo("t", float(g % 360), isocentro_mm=iso)
        ua, va = projetar_bev(pa, c, sad_mm)
        if len(p) == 0:
            return 0, ua, va
        up, vp = projetar_bev(p, c, sad_mm)
        bins = np.arange(va.min() - margem_mm, va.max() + margem_mm + 10, 10.0)
        ia, ip = np.digitize(va, bins), np.digitize(vp, bins)
        n = 0
        for b in np.unique(ia):
            s = ia == b
            lo, hi = ua[s].min() - margem_mm, ua[s].max() + margem_mm
            n += int(((ip == b) & (up >= lo) & (up <= hi)).sum())
        return n, ua, va

    melhor = None
    for g1 in faixa:
        _, ua, _ = no_campo(g1, np.zeros((0, 3)))
        for sinal in (+1, -1):
            # borda posterior: o lado do BEV mais perto do pulmão
            nl1, _, _ = no_campo(g1, pl)
            d = max(abs(ua.min()), abs(ua.max())) + margem_mm
            teta = np.degrees(2 * np.arctan(d / sad_mm))
            g2 = (g1 + 180 + sinal * teta) % 360
            nl2, _, _ = no_campo(g2, pl)
            nc = (no_campo(g1, pc)[0] + no_campo(g2, pc)[0]) if len(pc) else 0
            custo = (nl1 + nl2) / max(len(pl), 1) + 3.0 * nc / max(len(pc), 1)
            if melhor is None or custo < melhor[0]:
                melhor = (custo, float(g1), float(round(g2, 1)), nl1 + nl2, nc)
    _, g1, g2, nl, nc = melhor
    return {"medial": g1, "lateral": g2, "fracao_pulmao": round(nl / max(2 * len(pl), 1) * 100, 1),
            "fracao_coracao": round(nc / max(2 * len(pc), 1) * 100, 1) if len(pc) else None}
