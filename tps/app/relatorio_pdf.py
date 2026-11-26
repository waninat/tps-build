"""Relatório do plano em PDF (reportlab + figuras do matplotlib). SRS-EXP-040."""
from __future__ import annotations

import datetime as dt
import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from reportlab.lib import colors  # noqa: E402
from reportlab.lib.pagesizes import A4  # noqa: E402
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet  # noqa: E402
from reportlab.lib.units import mm  # noqa: E402
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,  # noqa: E402
                                TableStyle)

from tps.version import __version__  # noqa: E402

CORES_ISO = {50: "#00b7ff", 80: "#9be000", 95: "#ffae00", 100: "#ff7a00", 105: "#ff2d55", 110: "#ff00c8"}


def _algoritmo_txt(p) -> str:
    cd = getattr(p, "calculo_dose", None) or {}
    if cd.get("algoritmo") == "MC":
        inc = cd.get("incerteza_obtida_pct")
        return ("dose por Monte Carlo" + (f" (incerteza {inc:.2f}%, 1σ)" if inc is not None else "") +
                (", dose na água" if cd.get("dose_na_agua") else ", dose no meio"))
    return "dose por collapsed cone"


def _f(v, n=1):
    return "—" if v is None else f"{v:.{n}f}"


def _fig_axial(s) -> bytes:
    g, p = s.grade, s.plano
    iso = s.isocentro or (0, 0, 0)
    k = int(np.argmin(np.abs(g.z - iso[2])))
    fig, ax = plt.subplots(figsize=(6.2, 4.6), dpi=150)
    ax.imshow(np.clip(g.eta[k], 0, 1.6), cmap="gray", extent=[g.x[0], g.x[-1], g.y[-1], g.y[0]], vmin=0, vmax=1.6)
    for n, e in s.estruturas.items():
        if e.mascara[k].any():
            ax.contour(g.x, g.y, e.mascara[k].astype(float), levels=[0.5], colors=[np.array(e.cor) / 255],
                       linewidths=1.0)
    if s.dose_atualizada:
        rel = s.dose_fx[k] / p.prescricao.dose_fracao_gy * 100
        cs = ax.contour(g.x, g.y, rel, levels=list(CORES_ISO), colors=list(CORES_ISO.values()), linewidths=1.1)
        ax.clabel(cs, fmt="%d%%", fontsize=6)
    ax.plot(iso[0], iso[1], "r+", ms=10)
    ax.set_title(f"Axial no isocentro (z = {g.z[k]:.1f} mm)", fontsize=9)
    ax.set_xlabel("x (mm)", fontsize=8)
    ax.set_ylabel("y (mm)", fontsize=8)
    ax.tick_params(labelsize=7)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _fig_dvh(s) -> bytes:
    fig, ax = plt.subplots(figsize=(6.2, 3.2), dpi=150)
    for n, h in s.resultado["dvh"].items():
        if "dose_gy" in h:
            ax.plot(h["dose_gy"], h["volume_pct"], color=np.array(s.estruturas[n].cor) / 255, label=n, lw=1.3)
    ax.axvline(s.plano.prescricao.dose_total_gy, color="gray", ls=":", lw=0.8)
    ax.set_xlabel("Dose (Gy)", fontsize=8)
    ax.set_ylabel("Volume (%)", fontsize=8)
    ax.set_ylim(0, 102)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=6, ncol=3)
    ax.tick_params(labelsize=7)
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def relatorio_pdf(s, usuario: dict | None = None) -> bytes:
    p = s.plano
    if p is None:
        raise ValueError("Sem plano")
    est = getSampleStyleSheet()
    normal = ParagraphStyle("n", parent=est["Normal"], fontSize=8.5, leading=11)
    titulo = ParagraphStyle("t", parent=est["Heading1"], fontSize=14, spaceAfter=4)
    sub = ParagraphStyle("s", parent=est["Heading2"], fontSize=10.5, spaceBefore=8, spaceAfter=4)
    aviso = ParagraphStyle("a", parent=normal, textColor=colors.HexColor("#8a5a00"),
                           backColor=colors.HexColor("#fff3cd"), borderPadding=4)
    estilo_tab = TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7.5), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf3")),
                             ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])
    pr = p.prescricao
    r = s.resultado if s.dose_atualizada else {}
    el = [Paragraph(f"Plano {p.rotulo} — {s.paciente.get('nome', '')} ({s.paciente.get('id', '')})", titulo),
          Paragraph(f"Software em desenvolvimento (TPS {__version__}) — não liberado para uso clínico.", aviso),
          Spacer(1, 4),
          Paragraph(f"Máquina <b>{s.maquina.id}</b> ({p.nome_dicom_maquina}), {s.maquina.energia}, MLC "
                    f"{s.maquina.colim.nome} · técnica {p.tecnica} · {_algoritmo_txt(p)} · modelo de feixe "
                    f"{p.modelo_feixe.get('hash', '')} "
                    f"({(s.maquina.modelo.ref or {}).get('comissionamento', 'v1')}) · isocentro (mm) "
                    f"{', '.join(_f(v) for v in (s.isocentro or ()))} · emitido em {dt.datetime.now():%d/%m/%Y %H:%M}"
                    + (f" por {usuario['nome']}" if usuario else ""), normal),
          Paragraph("Prescrição", sub),
          Paragraph(f"<b>{pr.dose_total_gy:g} Gy em {pr.fracoes} frações</b> ({pr.dose_fracao_gy:.3f} Gy/fx) em "
                    f"{pr.estrutura_alvo or 'ponto'}; normalização {pr.metodo}"
                    f"{' em ' + str(pr.ponto_mm) if pr.ponto_mm else ''}; isodose {pr.isodose_pct:g}%. "
                    f"HI {_f(r.get('hi'), 3)} · Dmax {_f(r.get('dmax_gy'), 2)} Gy.", normal)]
    if p.densidades or p.bolus:
        txt = []
        if p.densidades:
            txt.append("Densidades forçadas: " + ", ".join(f"{k} = {v:g}" for k, v in p.densidades.items()))
        if p.bolus:
            txt.append("<b>BOLUS:</b> " + "; ".join(f"{b['nome']} ({b['espessura_mm']:g} mm, campos "
                                                    f"{', '.join(b['campos'])})" for b in p.bolus))
        el.append(Paragraph(" · ".join(txt), normal))
    el.append(Paragraph("Campos", sub))
    linhas = [["Campo", "Pai", "Gantry", "Colim.", "Mesa", "Peso", "UM", "SSD (cm)", "Jaw X (mm)", "Jaw Y (mm)", "Seg."]]
    for c in p.campos:
        linhas.append([c.nome, c.campo_pai or "", f"{c.gantry:.1f}", f"{c.colimador:.1f}", f"{c.mesa:.1f}",
                       _f(c.peso, 3), _f(c.mu), _f(c.ssd_mm / 10 if c.ssd_mm else None),
                       f"{_f(c.jaw_x[0])} / {_f(c.jaw_x[1])}" if c.jaw_x else "—",
                       f"{_f(c.jaw_y[0])} / {_f(c.jaw_y[1])}", str(len(c.segmentos) if c.segmentos else 1)])
    t = Table(linhas, repeatRows=1)
    t.setStyle(estilo_tab)
    el.append(t)
    if r.get("conferencia_um"):
        el.append(Paragraph("Conferência independente de UM (tabelas TMR/Sc/Sp/OAR)", sub))
        lin = [["Campo", "Motor (cGy/UM)", "Tabela (cGy/UM)", "Diferença", "Situação"]]
        for k in r["conferencia_um"]:
            lin.append([k["campo"], f"{k['motor_cgy_um']:.4f}", f"{k['tabela_cgy_um']:.4f}", f"{k['dif_pct']:+.2f}%",
                        "dentro de 3%" if k["ok"] else "FORA DA TOLERÂNCIA"])
        t = Table(lin, repeatRows=1)
        t.setStyle(estilo_tab)
        el.append(t)
    if s.dose_atualizada:
        el.append(KeepTogether([Paragraph("Distribuição de dose", sub), Image(io.BytesIO(_fig_axial(s)), 150 * mm, 111 * mm)]))
        el.append(KeepTogether([Paragraph("Histograma dose-volume", sub), Image(io.BytesIO(_fig_dvh(s)), 150 * mm, 77 * mm)]))
        lin = [["Estrutura", "Volume (cc)", "Dmin", "Dmédia", "Dmax", "D98", "D95", "D2"]]
        for n, h in r["dvh"].items():
            lin.append([n, _f(h.get("volume_cc")), *(_f(h.get(k), 2) for k in ("Dmin", "Dmean", "Dmax", "D98", "D95", "D2"))])
        t = Table(lin, repeatRows=1)
        t.setStyle(estilo_tab)
        el.append(t)
        rr = r.get("restricoes") or {}
        if rr.get("resultados"):
            el.append(Paragraph(f"Restrições de dose — {rr.get('protocolo', '')}", sub))
            for a in rr.get("avisos", []):
                el.append(Paragraph(f"<font color='#b06000'>{a}</font>", normal))
            lin = [["Estrutura", "TG-263", "Métrica", "Limite", "Valor", "Tipo", "Situação", "Fonte"]]
            for q in rr["resultados"]:
                sit = "ok" if q["ok"] else ("NÃO CUMPRE" if q["tipo"] == "limite" else "acima do objetivo")
                lin.append([q["estrutura"][:22], q["tg263"], q["metrica"], f"{q['op']} {q['limite']:g} {q['unidade']}",
                            f"{q['valor']:.2f}", q["tipo"], sit, Paragraph(f"<font size=6>{q['fonte']}</font>", normal)])
            t = Table(lin, repeatRows=1, colWidths=[30 * mm, 22 * mm, 18 * mm, 28 * mm, 14 * mm, 14 * mm, 24 * mm, 30 * mm])
            t.setStyle(estilo_tab)
            el.append(t)
        pts = [x for x in (s.resumo()["plano"]["pontos"] or [])]
        if pts:
            el.append(Paragraph("Pontos de referência (dose do plano completo)", sub))
            lin = [["Ponto", "x, y, z (mm)", "Dose (Gy)", "% prescr."] + [c.nome for c in p.campos][:8]]
            for q in pts:
                lin.append([q["nome"], ", ".join(f"{v:.1f}" for v in q["xyz"]), _f(q["total_gy"], 2), _f(q["pct"])]
                           + [_f(q["campos"].get(c.nome), 2) for c in p.campos][:8])
            t = Table(lin, repeatRows=1)
            t.setStyle(estilo_tab)
            el.append(t)
    el.append(Paragraph("Aprovação", sub))
    ap = p.aprovacao
    el.append(Paragraph(f"Aprovado por <b>{ap['usuario']}</b> ({ap['papel']}) em {ap['quando']}<br/>"
                        f"<font size=7>hash do plano: {ap['hash']}</font>" if ap else
                        "<font color='#b00020'><b>PLANO NÃO APROVADO</b></font>", normal))
    ju = (ap or {}).get("conferencia_um_justificativa")
    if ju:
        campos_ju = ", ".join(f"{x['campo']} {x['dif_pct']:+.1f}%" for x in ju["campos"])
        el.append(Paragraph("<font color='#b06000'><b>Aprovado com conferência independente de UM fora da "
                            f"tolerância</b> ({campos_ju}) — justificativa de {ju['usuario']}: "
                            f"{ju['justificativa']}</font>", normal))
    jc = (ap or {}).get("checklist_justificativa")
    if jc:
        el.append(Paragraph("<font color='#b06000'><b>Aprovado com pendências no checklist</b> ("
                            f"{', '.join(jc['itens'])}) — justificativa de {jc['usuario']}: {jc['justificativa']}</font>",
                            normal))
    try:
        from tps.app.checklist import checklist
        chk = checklist(s) if s.dose_atualizada else []
    except Exception:  # noqa: BLE001
        chk = []
    if chk:
        el.append(Paragraph("Checklist do plano", sub))
        lin = [["", "Item", "Detalhe"]]
        for i in chk:
            lin.append([{"ok": "OK", "aviso": "!", "erro": "X"}[i["estado"]], i["titulo"],
                        Paragraph(f"<font size=7>{i['detalhe']}</font>", normal)])
        t = Table(lin, repeatRows=1, colWidths=[8 * mm, 60 * mm, 112 * mm])
        t.setStyle(estilo_tab)
        el.append(t)
    if s.dose_atualizada and p.tecnica in ("3D", "DCA"):
        try:
            cq = s.cq_por_calculo()
        except Exception:  # noqa: BLE001
            cq = None
        if cq and cq.get("itens"):
            el.append(Paragraph("Complexidade e CQ por cálculo independente — "
                                + ("dentro dos critérios" if cq["elegivel"] else "fora dos critérios (CQ por medida)")
                                + ("" if cq.get("confirmado") else " (critérios ainda não confirmados pela clínica)"), sub))
            lin = [["Critério", "Plano", "Limite", ""]]
            for i in cq["itens"]:
                lin.append([Paragraph(f"<font size=7>{i['nome']}</font>", normal),
                            "—" if i["valor"] is None else f"{i['valor']:g} {i['unidade']}", i["limite"],
                            "OK" if i["ok"] else "X"])
            t = Table(lin, repeatRows=1, colWidths=[90 * mm, 35 * mm, 45 * mm, 10 * mm])
            t.setStyle(estilo_tab)
            el.append(t)
    if p.historico:
        el.append(Paragraph("Histórico: " + " · ".join(f"{h['evento']} {h.get('usuario', '')} {h.get('quando', '')}"
                                                       for h in p.historico), normal))
    buf = io.BytesIO()

    def rodape(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawString(15 * mm, 8 * mm, f"TPS {__version__} — {p.rotulo} — {s.paciente.get('id', '')}")
        canvas.drawRightString(195 * mm, 8 * mm, f"página {doc.page}")
        canvas.restoreState()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm,
                      bottomMargin=14 * mm, title=f"Plano {p.rotulo}").build(el, onFirstPage=rodape, onLaterPages=rodape)
    return buf.getvalue()


# ------------------------------------------------------------------ relatório para o médico
def _fig_tres_vistas(s) -> bytes:
    """Axial, coronal e sagital pelo isocentro: CT, contornos, colorwash da dose e isodoses 95/100/105/107%."""
    g, p = s.grade, s.plano
    cx, cy, cz = s.ct.axes()
    iso = np.asarray(s.isocentro or p.campos[0].isocentro_mm, float)
    rx = p.prescricao.dose_total_gy
    tot = s.dose_fx * p.prescricao.fracoes / rx * 100 if s.dose_atualizada else None
    fig, axs = plt.subplots(1, 3, figsize=(10.5, 3.9), dpi=150, gridspec_kw={"width_ratios": [1.2, 1, 1]})
    niveis = [95, 100, 105, 107]
    cores = ["#00c8ff", "#ffae00", "#ff2d55", "#ff00c8"]
    vistas = [("axial", int(np.argmin(np.abs(cz - iso[2]))), int(np.argmin(np.abs(g.z - iso[2])))),
              ("coronal", int(np.argmin(np.abs(cy - iso[1]))), int(np.argmin(np.abs(g.y - iso[1])))),
              ("sagital", int(np.argmin(np.abs(cx - iso[0]))), int(np.argmin(np.abs(g.x - iso[0]))))]
    for ax, (v, kc, kg) in zip(axs, vistas):
        if v == "axial":
            img, ext, hh, vv = s.ct.hu[kc], [cx[0], cx[-1], cy[-1], cy[0]], (g.x, g.y), None
            dd = tot[kg] if tot is not None else None
            masc = lambda m: m[kg]  # noqa: E731
            pi = (iso[0], iso[1])
        elif v == "coronal":
            img, ext = s.ct.hu[:, kc, :], [cx[0], cx[-1], cz[0], cz[-1]]
            hh = (g.x, g.z)
            dd = tot[:, kg, :] if tot is not None else None
            masc = lambda m: m[:, kg, :]  # noqa: E731
            pi = (iso[0], iso[2])
        else:
            img, ext = s.ct.hu[:, :, kc], [cy[0], cy[-1], cz[0], cz[-1]]
            hh = (g.y, g.z)
            dd = tot[:, :, kg] if tot is not None else None
            masc = lambda m: m[:, :, kg]  # noqa: E731
            pi = (iso[1], iso[2])
        origem = "upper" if v == "axial" else "lower"
        ax.imshow(np.clip(img, -200, 300), cmap="gray", extent=ext, origin=origem if v != "axial" else None,
                  aspect="equal")
        if dd is not None:
            wash = np.ma.masked_less(dd, 50)
            ax.imshow(wash, cmap="jet", alpha=0.35, vmin=50, vmax=max(110, float(np.nanmax(dd))),
                      extent=[hh[0][0], hh[0][-1], hh[1][-1], hh[1][0]] if v == "axial" else
                      [hh[0][0], hh[0][-1], hh[1][0], hh[1][-1]], origin=None if v == "axial" else "lower")
            ax.contour(hh[0], hh[1], dd, levels=niveis, colors=cores, linewidths=0.9)
        for n, e in s.estruturas.items():
            if e.tipo.upper() == "EXTERNAL":
                continue
            m = masc(e.mascara)
            if m.any():
                ax.contour(hh[0], hh[1], m.astype(float), levels=[0.5], colors=[np.array(e.cor) / 255],
                           linewidths=0.8)
        ax.plot(*pi, "r+", ms=8)
        ax.set_title(v.capitalize(), fontsize=8)
        ax.set_xticks([])
        ax.set_yticks([])
        if v == "axial":
            ax.set_ylim(ext[2], ext[3])
    from matplotlib.lines import Line2D
    fig.legend([Line2D([0], [0], color=c, lw=1.2) for c in cores], [f"{n}%" for n in niveis], loc="lower center",
               ncol=4, fontsize=7, frameon=False)
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def relatorio_medico_pdf(s, usuario: dict | None = None) -> bytes:
    """Resumo para o médico aprovar ou pedir mudanças sem abrir o TPS: 1–2 páginas."""
    p = s.plano
    if p is None or not s.dose_atualizada:
        raise ValueError("Calcule a dose antes de gerar o relatório para o médico")
    est = getSampleStyleSheet()
    normal = ParagraphStyle("n", parent=est["Normal"], fontSize=8.5, leading=11)
    titulo = ParagraphStyle("t", parent=est["Heading1"], fontSize=13, spaceAfter=2)
    sub = ParagraphStyle("s", parent=est["Heading2"], fontSize=10, spaceBefore=6, spaceAfter=3)
    estilo_tab = TableStyle([("FONTSIZE", (0, 0), (-1, -1), 7.5), ("GRID", (0, 0), (-1, -1), 0.3, colors.grey),
                             ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf3")),
                             ("ALIGN", (1, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")])
    pr = p.prescricao
    r = s.resultado
    corpo = s._corpo()
    el = [Paragraph(f"{s.paciente.get('nome', '')} ({s.paciente.get('id', '')}) — plano {p.rotulo}", titulo),
          Paragraph(f"<b>{pr.dose_total_gy:g} Gy em {pr.fracoes} frações</b> ({pr.dose_fracao_gy:.2f} Gy/fx) · alvo "
                    f"{pr.estrutura_alvo or '—'} · {len([c for c in p.campos if not c.campo_pai])} campos "
                    f"({', '.join(f'{c.gantry:.0f}°' for c in p.campos if not c.campo_pai)}) · {s.maquina.energia} · "
                    f"{_algoritmo_txt(p)} · "
                    f"Dmax {r['dmax_gy']:.2f} Gy ({r['dmax_gy'] / pr.dose_total_gy * 100:.1f}%) · "
                    f"{'APROVADO pela física' if p.aprovacao else 'em revisão'} · {dt.datetime.now():%d/%m/%Y %H:%M}"
                    f" · TPS {__version__} (não liberado para uso clínico)", normal),
          Spacer(1, 3), Image(io.BytesIO(_fig_tres_vistas(s)), 180 * mm, 67 * mm)]
    # cobertura do alvo
    if pr.estrutura_alvo and pr.estrutura_alvo in r["dvh"]:
        h = r["dvh"][pr.estrutura_alvo]
        m = s.estruturas[pr.estrutura_alvo].mascara
        tot = s.dose_fx * pr.fracoes
        v95 = float((tot[m] >= 0.95 * pr.dose_total_gy).mean() * 100)
        lin = [["Alvo", "V95%", "D98", "D95", "D50", "D2", "Dmax"],
               [pr.estrutura_alvo, f"{v95:.1f}%", *(f"{h.get(k, float('nan')):.2f} Gy" for k in ("D98", "D95", "D50", "D2",
                                                                                                   "Dmax"))]]
        t = Table(lin)
        t.setStyle(estilo_tab)
        el += [Paragraph("Cobertura", sub), t]
    el.append(KeepTogether([Paragraph("Histograma dose-volume", sub), Image(io.BytesIO(_fig_dvh(s)), 160 * mm, 70 * mm)]))
    rr = (r.get("restricoes") or {}).get("resultados") or []
    orgaos = [q for q in rr if q.get("tg263") != "alvo"]
    if orgaos:
        lin = [["Órgão", "Métrica", "Limite", "Valor", ""]]
        estilos = list(estilo_tab.getCommands())
        for i, q in enumerate(orgaos, start=1):
            sit = "OK" if q["ok"] else ("NÃO CUMPRE" if q["tipo"] == "limite" else "acima do objetivo")
            cor = "#d9f2d9" if q["ok"] else ("#f8d0d0" if q["tipo"] == "limite" else "#fff0c0")
            estilos.append(("BACKGROUND", (4, i), (4, i), colors.HexColor(cor)))
            lin.append([q["estrutura"][:24], q["metrica"], f"{q['op'].replace('<=', '≤').replace('>=', '≥')} "
                        f"{q['limite']:g} {q['unidade'] if q['unidade'] != 'Gy' else 'Gy'}", f"{q['valor']:.2f}", sit])
        t = Table(lin, repeatRows=1, colWidths=[45 * mm, 25 * mm, 35 * mm, 22 * mm, 30 * mm])
        t.setStyle(TableStyle(estilos))
        el += [Paragraph("Órgãos de risco", sub), t]
    try:
        from tps.app.checklist import checklist
        pend = [i for i in checklist(s) if i["estado"] != "ok"]
    except Exception:  # noqa: BLE001
        pend = []
    if pend:
        el.append(Paragraph("Pontos de atenção da física", sub))
        for i in pend:
            el.append(Paragraph(f"{'✗' if i['estado'] == 'erro' else '!'} <b>{i['titulo']}</b>: {i['detalhe']}", normal))
    el += [Spacer(1, 10), Paragraph("Parecer médico", sub),
           Paragraph("(  ) Aprovo o plano &nbsp;&nbsp;&nbsp; (  ) Solicito alterações: "
                     "________________________________________________", normal), Spacer(1, 14),
           Paragraph("Assinatura / CRM: ______________________________ &nbsp;&nbsp; Data: ____/____/______", normal)]
    buf = io.BytesIO()

    def rodape(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.drawString(15 * mm, 8 * mm, f"Relatório para o médico — {p.rotulo} — {s.paciente.get('id', '')}")
        canvas.drawRightString(195 * mm, 8 * mm, f"página {doc.page}")
        canvas.restoreState()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm,
                      bottomMargin=14 * mm, title=f"Plano {p.rotulo} — médico").build(el, onFirstPage=rodape,
                                                                                    onLaterPages=rodape)
    return buf.getvalue()
