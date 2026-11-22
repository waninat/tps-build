"""Folha de conferência por filme (check film): um DRR por campo com mandíbulas, abertura do MLC, estruturas
projetadas, retícula de 1 cm e escala. PDF A4, uma página por campo (e por campo de setup, se houver).

O DRR é calculado na resolução do CT (não na grade de dose), integrando a atenuação relativa à água ao longo de cada
raio, da fonte até o plano do isocentro e além. A imagem e as marcações estão no plano do isocentro (SAD 100 cm); a
escala impressa diz quantos mm do papel valem 1 cm no isocentro.
"""
from __future__ import annotations

import io

import numpy as np
from scipy.ndimage import map_coordinates

from tps.engine.geometry import beam_frame


def drr_ct(ct, campo, ext_mm: float, n: int = 400, passo_mm: float = 1.5) -> np.ndarray:
    """DRR [n, n] (linha 0 = +v, coluna 0 = −u) no plano do isocentro, de −ext a +ext mm."""
    fr = beam_frame(campo.gantry, campo.colimador, campo.mesa, campo.isocentro_mm, 1000.0, "HFS")
    uu = np.linspace(-ext_mm, ext_mm, n)
    U, V = np.meshgrid(uu, uu[::-1])
    P = fr.source + 1000.0 * fr.w + U[..., None] * fr.u + V[..., None] * fr.v
    d = P - fr.source
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    x, y, z = ct.axes()
    mu = np.clip((ct.hu.astype(np.float32) + 1000.0) / 1000.0, 0, None)
    mu = np.where(ct.hu > 150, mu * (1 + (ct.hu - 150) / 1500.0), mu)       # realce discreto de osso
    cantos = np.array([[a, b, c] for a in (x[0], x[-1]) for b in (y[0], y[-1]) for c in (z[0], z[-1])])
    dist = np.linalg.norm(cantos - fr.source, axis=1)
    t0, t1 = max(0.0, dist.min() - 5), dist.max() + 5
    img = np.zeros((n, n), np.float32)
    for t in np.arange(t0, t1, passo_mm):
        q = fr.source + t * d
        img += map_coordinates(mu, [(q[..., 2] - z[0]) / (z[1] - z[0]), (q[..., 1] - y[0]) / (y[1] - y[0]),
                                    (q[..., 0] - x[0]) / (x[1] - x[0])], order=1, mode="constant", cval=0.0)
    img *= passo_mm
    dentro = img[img > 1.0]
    if dentro.size:
        lo, hi = np.percentile(dentro, 1), np.percentile(dentro, 99.8)
        img = np.clip((img - lo) / max(hi - lo, 1e-6), 0, 1) ** 1.3 * (img > 1.0)
    return img


def _abertura(limites, a, b, jaw_x, jaw_y, ext, res=0.5):
    uu = np.arange(-ext, ext + res, res)
    vv = np.arange(-ext, ext + res, res)
    U, V = np.meshgrid(uu, vv)
    m = (V >= jaw_y[0]) & (V <= jaw_y[1])
    if jaw_x:
        m &= (U >= jaw_x[0]) & (U <= jaw_x[1])
    if a is not None:
        aberto = np.zeros_like(m)
        for i in range(len(limites) - 1):
            faixa = (V >= limites[i]) & (V < limites[i + 1])
            aberto |= faixa & (U >= a[i]) & (U <= b[i])
        m &= aberto
    return uu, vv, m.astype(float)


def folha_pdf(sessao) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    s, p = sessao, sessao.plano
    colim = s.maquina.colim
    lim = np.asarray(colim.limites_laminas, float)
    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        for c in p.campos:
            if c.campo_pai:
                continue                              # subcampos (campo dentro do campo) entram na página do pai
            filhos = [f for f in p.campos if f.campo_pai == c.nome]
            mx = max([abs(v) for v in c.jaw_y] + ([abs(v) for v in c.jaw_x] if c.jaw_x else
                                                  [abs(np.nanmin(c.mlc_a)), abs(np.nanmax(c.mlc_b))]))
            ext = float(np.clip(mx * 1.35 + 15, 60, 220))
            img = drr_ct(s.ct, c, ext)
            # escala no papel: 1:1 no isocentro se couber em 170 mm, senão reduz
            esc = min(1.0, 170.0 / (2 * ext))
            fig = plt.figure(figsize=(210 / 25.4, 297 / 25.4))
            lado = 2 * ext * esc / 210.0
            ax = fig.add_axes([(1 - lado) / 2, 0.885 - 2 * ext * esc / 297.0, lado, 2 * ext * esc / 297.0])
            ax.imshow(img, cmap="gray_r", extent=[-ext, ext, -ext, ext], interpolation="bilinear")
            # estruturas projetadas
            bev = s.bev(c.nome, res_mm=1.0)
            for pr in bev["projecoes"]:
                cor = np.array([int(v) for v in str(pr["cor"]).replace(" ", "").split(",")[:3]]) / 255 \
                    if isinstance(pr["cor"], str) else np.array(pr["cor"][:3]) / 255
                for ln in pr["linhas"]:
                    ln = np.asarray(ln)
                    if len(ln) > 1:
                        ax.plot(ln[:, 0], ln[:, 1], color=cor, lw=0.9)
            # mandíbulas e MLC (envelope e, se houver, segmentos)
            ax.plot([-ext, ext], [c.jaw_y[0]] * 2, color="#e07000", lw=1.0, ls="--")
            ax.plot([-ext, ext], [c.jaw_y[1]] * 2, color="#e07000", lw=1.0, ls="--")
            if c.jaw_x:
                for u in c.jaw_x:
                    ax.plot([u, u], [-ext, ext], color="#0070c0", lw=1.0, ls="--")
            uu, vv, m = _abertura(lim, c.mlc_a, c.mlc_b, c.jaw_x, c.jaw_y, ext)
            ax.contour(uu, vv, m, levels=[0.5], colors="#c00000", linewidths=1.6)
            for f in filhos:
                uu, vv, m = _abertura(lim, f.mlc_a, f.mlc_b, f.jaw_x, f.jaw_y, ext)
                ax.contour(uu, vv, m, levels=[0.5], colors="#c00000", linewidths=0.8, linestyles=":")
            # retícula 1 cm
            k = np.arange(-int(ext // 10) * 10, ext + 1e-6, 10)
            ax.plot([-ext, ext], [0, 0], color="k", lw=0.5)
            ax.plot([0, 0], [-ext, ext], color="k", lw=0.5)
            for t in k:
                tam = 3 if t % 50 else 6
                ax.plot([t, t], [-tam / 2, tam / 2], color="k", lw=0.5)
                ax.plot([-tam / 2, tam / 2], [t, t], color="k", lw=0.5)
            # barra de 10 cm
            x0 = -ext + 8
            ax.plot([x0, x0 + 100], [-ext + 8] * 2, color="k", lw=2.5)
            ax.text(x0 + 50, -ext + 11, "10 cm no isocentro", ha="center", fontsize=7)
            ax.text(-ext + 4, ext - 8, "Y2", fontsize=8, color="#e07000")
            ax.text(-ext + 4, -ext + 20, "Y1", fontsize=8, color="#e07000")
            ax.text(-ext + 4, 4, "X1 (A)", fontsize=8)
            ax.text(ext - 22, 4, "X2 (B)", fontsize=8)
            ax.set_xlim(-ext, ext)
            ax.set_ylim(-ext, ext)
            ax.set_xticks([])
            ax.set_yticks([])
            iso = c.isocentro_mm
            linhas = [
                f"{s.paciente.get('nome', '')}   ID {s.paciente.get('id', '')}   Plano {p.rotulo}   ({p.estado})",
                f"Campo {c.nome}   Máquina {s.maquina.nome_dicom} {s.maquina.energia}   "
                f"Gantry {c.gantry:.1f}°   Colimador {c.colimador:.1f}°   Mesa {c.mesa:.1f}°",
                f"UM {c.mu:.1f}" if c.mu is not None else "UM —",
                f"Isocentro (mm, DICOM): {iso[0]:.1f}, {iso[1]:.1f}, {iso[2]:.1f}   SSD "
                + (f"{c.ssd_mm / 10:.1f} cm" if c.ssd_mm else "—"),
                f"Y1/Y2 {c.jaw_y[0]:.1f}/{c.jaw_y[1]:.1f} mm" + (f"   X1/X2 {c.jaw_x[0]:.1f}/{c.jaw_x[1]:.1f} mm"
                                                                 if c.jaw_x else "")
                + (f"   {len(filhos)} subcampo(s) (pontilhado)" if filhos else "")
                + (f"   {len(c.segmentos)} segmentos" if c.segmentos else ""),
                f"Escala: 1 cm no isocentro = {10 * esc:.1f} mm no papel (imprimir em tamanho real, sem ajustar à página)",
            ]
            for i, t in enumerate(linhas):
                fig.text(0.07, 0.975 - i * 0.012, t, fontsize=8 if i else 9, weight="bold" if i in (0, 1) else None)
            fig.text(0.07, 0.03, "Vermelho: abertura do MLC   Laranja: mandíbulas Y   Azul: mandíbulas X   "
                                 "Imagem: DRR calculado do CT. Conferir contra o filme de verificação.", fontsize=7)
            fig.text(0.07, 0.018, "TPS em desenvolvimento: não usar em paciente.", fontsize=7, color="#b00020")
            pdf.savefig(fig)
            plt.close(fig)
    return buf.getvalue()
