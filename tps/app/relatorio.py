"""Relatório do plano (HTML para imprimir ou salvar como PDF pelo navegador). SRS-EXP-040."""
from __future__ import annotations

import datetime as dt
import html

from tps.version import __version__


def _f(v, n=1):
    return "—" if v is None else f"{v:.{n}f}"


def relatorio(s) -> str:
    p = s.plano
    if p is None:
        return "<p>Sem plano</p>"
    pr = p.prescricao
    r = s.resultado if s.dose_atualizada else {}
    e = html.escape
    linhas_campos = []
    for c in p.campos:
        linhas_campos.append(
            f"<tr><td>{e(c.nome)}</td><td>{e(c.campo_pai or '')}</td><td>{c.gantry:.1f}</td><td>{c.colimador:.1f}</td>"
            f"<td>{c.mesa:.1f}</td><td>{c.energia_mv:g} MV</td><td>{_f(c.peso, 3)}</td><td><b>{_f(c.mu)}</b></td>"
            f"<td>{_f(c.ssd_mm / 10 if c.ssd_mm else None)}</td>"
            f"<td>{_f(c.jaw_x[0]) + ' / ' + _f(c.jaw_x[1]) if c.jaw_x else '—'}</td>"
            f"<td>{_f(c.jaw_y[0])} / {_f(c.jaw_y[1])}</td><td>{len(c.segmentos) if c.segmentos else 1}</td></tr>")
    conf = "".join(
        f"<tr><td>{e(k['campo'])}</td><td>{k['motor_cgy_um']:.4f}</td><td>{k['tabela_cgy_um']:.4f}</td>"
        f"<td class='{'ok' if k['ok'] else 'falha'}'>{k['dif_pct']:+.2f}%</td>"
        f"<td>{'compensado: conferência por tabela é aproximada' if k.get('compensado') else ''}</td></tr>"
        for k in r.get("conferencia_um", []))
    est = ""
    for n, h in (r.get("dvh") or {}).items():
        est += (f"<tr><td>{e(n)}</td><td>{_f(h.get('volume_cc'))}</td><td>{_f(h.get('Dmin'), 2)}</td>"
                f"<td>{_f(h.get('Dmean'), 2)}</td><td>{_f(h.get('Dmax'), 2)}</td><td>{_f(h.get('D98'), 2)}</td>"
                f"<td>{_f(h.get('D95'), 2)}</td><td>{_f(h.get('D2'), 2)}</td></tr>")
    ap = p.aprovacao
    aprov = (f"Aprovado por <b>{e(ap['usuario'])}</b> ({e(ap['papel'])}) em {e(ap['quando'])}<br>"
             f"<small>hash do plano: {ap['hash']}</small>") if ap else "<b class='falha'>NÃO APROVADO</b>"
    return f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><title>Relatório {e(p.rotulo)}</title>
<style>body{{font:12px system-ui,sans-serif;margin:24px;color:#111}}h1{{font-size:18px;margin:0 0 4px}}
h2{{font-size:14px;margin:18px 0 6px;border-bottom:1px solid #999}}table{{border-collapse:collapse;width:100%}}
td,th{{border:1px solid #bbb;padding:3px 6px;text-align:right}}td:first-child,th:first-child{{text-align:left}}
.ok{{color:#0a7a2f}}.falha{{color:#b00020}}.aviso{{background:#fff3cd;padding:6px;border:1px solid #e0c060}}
@media print{{button{{display:none}}}}</style></head><body>
<button onclick="print()">Imprimir / salvar PDF</button>
<div class="aviso">Software em desenvolvimento (TPS {__version__}) — não liberado para uso clínico. Confira todo plano
por cálculo independente e medida.</div>
<h1>Plano {e(p.rotulo)} — {e(s.paciente.get('nome', ''))} ({e(s.paciente.get('id', ''))})</h1>
<div>Máquina: {e(s.maquina.id)} ({e(p.nome_dicom_maquina)}), {e(s.maquina.energia)}, MLC {e(s.maquina.colim.nome)} ·
Técnica: {e(p.tecnica)} · Modelo de feixe {e(str(p.modelo_feixe.get('hash', '')))} ·
Isocentro (mm): {', '.join(_f(v) for v in (s.isocentro or ()))} · Emitido em {dt.datetime.now():%d/%m/%Y %H:%M}</div>
<h2>Prescrição</h2>
<div>{pr.dose_total_gy:g} Gy em {pr.fracoes} frações ({pr.dose_fracao_gy:.3f} Gy/fração) em {e(pr.estrutura_alvo)};
normalização: {e(pr.metodo)}{' em ' + str(pr.ponto_mm) if pr.ponto_mm else ''}; isodose {pr.isodose_pct:g}%.
HI do alvo: {_f(r.get('hi'), 3)} · Dmax: {_f(r.get('dmax_gy'), 2)} Gy</div>
<h2>Campos</h2><table><tr><th>Campo</th><th>Pai</th><th>Gantry</th><th>Colim.</th><th>Mesa</th><th>Energia</th>
<th>Peso</th><th>UM</th><th>SSD (cm)</th><th>Jaw X (mm)</th><th>Jaw Y (mm)</th><th>Segmentos</th></tr>
{''.join(linhas_campos)}</table>
<h2>Conferência independente de UM (tabelas TMR/Sc/Sp)</h2><table><tr><th>Campo</th><th>Motor (cGy/UM)</th>
<th>Tabela (cGy/UM)</th><th>Diferença</th><th></th></tr>{conf}</table>
<h2>Estatísticas de dose (Gy, plano completo)</h2><table><tr><th>Estrutura</th><th>Volume (cc)</th><th>Dmin</th>
<th>Dmédia</th><th>Dmax</th><th>D98</th><th>D95</th><th>D2</th></tr>{est}</table>
<h2>Aprovação</h2><div>{aprov}</div>
<h2>Histórico</h2><div>{'<br>'.join(e(str(h)) for h in p.historico) or '—'}</div>
</body></html>"""
