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
                    f"{s.maquina.colim.nome} · técnica {p.tecnica} · modelo de feixe {p.modelo_feixe.get('hash', '')} "
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
