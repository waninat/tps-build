"""Checklist do plano antes da aprovação (no espírito do AAPM TG-275: o que costuma escapar na revisão). SRS-APR-010.

Cada item: {"id", "titulo", "estado": ok | aviso | erro, "detalhe"}. Itens "erro" não impedem a aprovação, mas
exigem justificativa escrita pelo físico (como a conferência de UM fora da tolerância).
"""
from __future__ import annotations

import numpy as np

from tps.planejamento import calculo

FOLGA_CABECOTE_MM = 400.0      # isocentro → face do colimador do Precise (sem acessório), com margem
RAIO_CABECOTE_MM = 300.0
UM_MIN_SEGMENTO = 2.0


def _item(i, titulo, estado, detalhe=""):
    return {"id": i, "titulo": titulo, "estado": estado, "detalhe": detalhe}


def colisoes(sessao) -> list[str]:
    """Campos cujo cabeçote passaria perto demais do corpo (pontos do corpo além da folga, dentro do raio)."""
    from tps.engine.geometry import beam_frame
    g = sessao.grade
    corpo = sessao._corpo()
    if not corpo.any():
        return []
    k = np.flatnonzero(corpo)
    if len(k) > 60000:
        k = np.random.default_rng(0).choice(k, 60000, replace=False)
    kz, ky, kx = np.unravel_index(k, corpo.shape)
    pts = np.column_stack([g.x[kx], g.y[ky], g.z[kz]])
    ruins = []
    for c in sessao.plano.campos + list(sessao.plano.campos_setup):
        if c.campo_pai:
            continue
        angulos = [c.gantry]
        if c.e_arco:                                          # arco: confere a cada 5° do caminho do gantry
            from tps.planejamento.vmat import angulos_cp
            angulos = list(angulos_cp(c.gantry, c.arco_fim, c.sentido, 5.0))
        for gan in angulos:
            fr = beam_frame(gan, c.colimador, c.mesa, c.isocentro_mm, 1000.0, "HFS")
            rel = pts - np.asarray(c.isocentro_mm, float)
            ao_longo = rel @ (-fr.w)                          # positivo = na direção da fonte
            radial = np.linalg.norm(rel - np.outer(rel @ fr.w, fr.w), axis=1)
            if np.any((ao_longo > FOLGA_CABECOTE_MM) & (radial < RAIO_CABECOTE_MM)):
                ruins.append(f"{c.nome} (G{gan:.0f} M{c.mesa:.0f})")
                break
    return ruins


def checklist(sessao) -> list[dict]:
    s = sessao
    p = s.plano
    if p is None:
        return []
    out = []
    pr = p.prescricao
    if not p.campos:
        return [_item("campos", "Campos de tratamento", "erro", "O plano não tem campos")]
    if not (s.dose_atualizada and s.dose_fx is not None):
        return [_item("dose", "Dose calculada com os campos atuais", "erro", "Calcule a dose")]
    out.append(_item("dose", "Dose calculada com os campos atuais", "ok",
                     f"calculada em {str(s.resultado.get('quando', ''))[:16].replace('T', ' ')}"))
    alvo = s.estruturas.get(pr.estrutura_alvo) if pr.estrutura_alvo else None
    m_alvo = alvo.mascara if alvo is not None else None
    if pr.metodo not in ("ponto", "isocentro", "um_fixas") and (m_alvo is None or not m_alvo.any()):
        out.append(_item("alvo", "Alvo da prescrição", "erro", f"{pr.estrutura_alvo or '—'} vazio ou inexistente"))
    # normalização: a métrica escolhida bate com a prescrição?
    if pr.metodo != "um_fixas":
        try:
            v = calculo.metrica_normalizacao(s.dose_fx, p, s.grade, m_alvo)
            alvo_v = pr.dose_fracao_gy * 100.0 / pr.isodose_pct
            dif = (v / alvo_v - 1) * 100
            out.append(_item("normalizacao", "Prescrição × normalização", "ok" if abs(dif) <= 1.0 else "erro",
                             f"{pr.metodo}: {v * pr.fracoes:.2f} Gy para {alvo_v * pr.fracoes:.2f} Gy ({dif:+.2f}%)"))
        except Exception as e:  # noqa: BLE001
            out.append(_item("normalizacao", "Prescrição × normalização", "erro", str(e)))
    total = s.dose_fx * pr.fracoes
    corpo = s._corpo()
    dmax = float(total[corpo].max()) if corpo.any() else float(total.max())
    pct = dmax / pr.dose_total_gy * 100
    out.append(_item("dmax", "Dose máxima", "ok" if pct <= 107 else ("aviso" if pct <= 110 else "erro"),
                     f"{dmax:.2f} Gy ({pct:.1f}%)"))
    if m_alvo is not None and m_alvo.any():
        d95 = float(np.percentile(total[m_alvo], 5)) / pr.dose_total_gy * 100
        out.append(_item("cobertura", f"Cobertura do alvo ({pr.estrutura_alvo})", "ok" if d95 >= 95 else "aviso",
                         f"D95 = {d95:.1f}% da prescrição"))
    # isocentro
    iso = np.asarray(p.campos[0].isocentro_mm, float)
    g = s.grade
    try:
        ii = [int(np.argmin(np.abs(ax - v))) for ax, v in ((g.z, iso[2]), (g.y, iso[1]), (g.x, iso[0]))]
        dentro = bool(corpo[tuple(ii)])
    except Exception:  # noqa: BLE001
        dentro = False
    out.append(_item("isocentro", "Isocentro dentro do corpo", "ok" if dentro else "aviso",
                     f"({iso[0]:.1f}, {iso[1]:.1f}, {iso[2]:.1f}) mm" + ("" if dentro else " — fora do corpo: confira")))
    # colisão
    try:
        col = colisoes(s)
        out.append(_item("colisao", "Colisão do cabeçote com o paciente", "erro" if col else "ok",
                         ("risco em " + ", ".join(col) + " — simule na máquina") if col else
                         "nenhum campo passa a menos de 40 cm do isocentro no corpo"))
    except Exception as e:  # noqa: BLE001
        out.append(_item("colisao", "Colisão do cabeçote com o paciente", "aviso", f"não verificado: {e}"))
    if any(abs(c.mesa) > 0.5 for c in p.campos if not c.campo_pai):
        out.append(_item("mesa", "Mesa girada", "aviso", "confira a colisão da mesa com o gantry na máquina"))
    # SSD e UM
    conf = s.resultado.get("conferencia_um") or []
    ssd_ruim = [f"{c.nome} {c.ssd_mm / 10:.1f} cm" for c in p.campos if getattr(c, "ssd_mm", None)
                and not 60 <= c.ssd_mm / 10 <= 120]
    out.append(_item("ssd", "SSD dos campos", "aviso" if ssd_ruim else "ok",
                     ", ".join(ssd_ruim) if ssd_ruim else "todas entre 60 e 120 cm"))
    poucas = [f"{c.nome} {c.mu:.1f}" for c in p.campos if (c.mu or 0) < UM_MIN_SEGMENTO]
    out.append(_item("um_min", f"UM ≥ {UM_MIN_SEGMENTO:g} em todos os campos e subcampos", "aviso" if poucas else "ok",
                     ", ".join(poucas)))
    fora = [k for k in conf if not k["ok"] and not k.get("compensado")]
    out.append(_item("conferencia_um", "Conferência independente de UM (±3%)", "erro" if fora else "ok",
                     ", ".join(f"{k['campo']} {k['dif_pct']:+.1f}%" for k in fora) if fora else
                     f"{len(conf)} campo(s) conferido(s)"))
    # restrições
    rr = (s.resultado.get("restricoes") or {}).get("resultados") or []
    limites = [q for q in rr if not q.get("ok") and q.get("tipo") == "limite"]
    objetivos = [q for q in rr if not q.get("ok") and q.get("tipo") != "limite"]
    out.append(_item("restricoes", "Restrições de dose (limites)", "erro" if limites else "ok",
                     "; ".join(f"{q['estrutura']} {q['metrica']} {q['valor']:.1f} (limite {q['limite']})"
                               for q in limites) or f"{len(rr)} avaliada(s)"))
    if objetivos:
        out.append(_item("objetivos", "Objetivos de dose não atingidos", "aviso",
                         "; ".join(f"{q['estrutura']} {q['metrica']} {q['valor']:.1f} (objetivo {q['limite']})"
                                   for q in objetivos)))
    # estruturas vazias com restrição própria
    vazias = [r["estrutura"] for r in p.restricoes if r["estrutura"] in s.estruturas
              and not s.estruturas[r["estrutura"]].mascara.any()]
    if vazias:
        out.append(_item("vazias", "Estruturas vazias com restrição", "aviso", ", ".join(vazias)))
    dens = getattr(p, "densidades", None) or {}
    if dens:
        out.append(_item("densidades", "Densidades forçadas", "aviso",
                         ", ".join(f"{n} = {v:g}" for n, v in dens.items()) + " — confirme que é intencional"))
    if p.bolus:
        out.append(_item("bolus", "Bolus no plano", "aviso", ", ".join(b["nome"] for b in p.bolus) +
                         " — precisa estar no paciente em todas as frações"))
    if p.tecnica == "IMRT":
        nseg = sum(len(c.segmentos or []) for c in p.campos)
        out.append(_item("imrt_qa", "IMRT: QA de paciente antes do 1º tratamento", "aviso",
                         f"{nseg} segmentos, {sum(float(c.mu or 0) for c in p.campos):.0f} UM — gere o plano de verificação "
                         "no fantoma e meça (gama)"))
    if p.tecnica == "VMAT":
        arcos = [c for c in p.campos if c.e_arco]
        out.append(_item("imrt_qa", "VMAT: QA de paciente antes do 1º tratamento", "aviso",
                         f"{len(arcos)} arco(s), {sum(float(c.mu or 0) for c in arcos):.0f} UM — gere o plano de "
                         "verificação no fantoma e meça (gama)"))
        from tps.planejamento.vmat import LimitesVMAT
        lim = LimitesVMAT.da_maquina(getattr(s.maquina, "raw", None))
        muitos = [f"{c.nome} {len(c.pontos_controle)}" for c in arcos
                  if c.pontos_controle and len(c.pontos_controle) > lim.max_pontos_arco]
        if muitos:
            out.append(_item("vmat_pontos", "VMAT: pontos de controle por arco", "erro",
                             f"{', '.join(muitos)} — acima do limite da máquina ({lim.max_pontos_arco:.0f}); aumente o "
                             "espaçamento dos pontos"))
        info = (s.resultado.get("info") or {}).get("vmat") or {}
        lentos = [f"{n} {a['tempo_s']:.0f} s" for n, a in (info.get("arcos") or {}).items()
                  if a.get("tempo_s", 0) > 240]
        if lentos:
            out.append(_item("vmat_tempo", "VMAT: tempo de entrega", "aviso", ", ".join(lentos)))
    if p.tecnica == "DCA":
        from tps.planejamento.vmat import LimitesVMAT, tempo_entrega
        lim = LimitesVMAT.da_maquina(getattr(s.maquina, "raw", None))
        tempos = [f"{c.nome} {tempo_entrega(c, lim)['tempo_s']:.0f} s" for c in p.campos if c.pontos_controle]
        out.append(_item("dca_tempo", "Arco conformacional: tempo de entrega", "ok", ", ".join(tempos)))
    if p.tecnica in ("3D", "DCA") and s.resultado:
        try:
            cq = s.cq_por_calculo()
        except Exception as e:  # noqa: BLE001
            cq = {"elegivel": False, "itens": [], "motivo": str(e)}
        falhas = [f"{i['nome']} ({i['valor'] if i['valor'] is not None else '—'}; limite {i['limite']})"
                  for i in cq.get("itens", []) if not i["ok"]]
        if cq.get("elegivel"):
            out.append(_item("cq_calculo", "Elegível para CQ só por cálculo independente",
                             "ok" if cq.get("confirmado") else "aviso",
                             "dentro dos critérios" + ("" if cq.get("confirmado") else
                                                       " (critérios da literatura, ainda não confirmados pela clínica)")))
        else:
            out.append(_item("cq_calculo", "CQ por medida antes do 1º tratamento", "aviso",
                             "fora dos critérios de CQ só por cálculo: " + ("; ".join(falhas) or cq.get("motivo", ""))))
    cd = getattr(p, "calculo_dose", None) or {}
    if cd.get("algoritmo") == "MC":
        inc = float(cd.get("incerteza_obtida_pct") or 0)
        out.append(_item("mc", "Monte Carlo: incerteza estatística", "ok" if inc <= 1.5 else ("aviso" if inc <= 3 else "erro"),
                         f"{inc:.2f}% (1σ, voxels > 50% da dose máxima) · "
                         f"{'dose na água' if cd.get('dose_na_agua') else 'dose no meio'}"))
        if p.tecnica == "VMAT" and not (s.maquina.modelo.mc_spectrum_hardening_k is not None):
            out.append(_item("mc_comissionamento", "Monte Carlo: comissionamento", "aviso",
                             "espectro e contaminação do MC ajustados ao modelo CCC (provisório) — falta validar com "
                             "medidas em água e em fantoma heterogêneo"))
    elif p.tecnica == "VMAT":
        out.append(_item("mc", "VMAT calculado com collapsed cone", "aviso",
                         "o padrão do VMAT é a dose final por Monte Carlo"))
    if not p.campos_setup:
        out.append(_item("setup", "Campos de localização", "aviso", "nenhum campo de setup (imagem de posicionamento)"))
    return out
