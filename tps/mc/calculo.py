"""Cálculo de dose por Monte Carlo: fonte, lotes, incerteza e calibração absoluta. SRS-MC-003.

Fonte: o MESMO modelo de feixe do collapsed cone (tps.engine.beam_model) — para cada segmento, a fluência de
energia no plano do isocentro (abertura com MLC/jaws, transmissões, ponta arredondada, tongue-and-groove,
borramento da fonte focal, fonte extrafocal, horn) é a distribuição de onde saem os fótons, que partem do ponto
focal com energia do espectro do modelo. A contaminação eletrônica é a mesma parcela analítica do CCC.

Calibração absoluta: como na câmara de ionização — num 10×10, DFS 100 cm, em água, a dose do MC no dmax é igualada
à do modelo comissionado (que reproduz a medida); o fator fica guardado por modelo de feixe.

Incerteza: lotes independentes; a incerteza relativa (1σ) é a média nos voxels acima de 50% da dose máxima.
Dose no meio (padrão) ou na água: D_w = D_m · (S/ρ)_w,m (razão de poderes de freamento por material).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass

import numba
import numpy as np

from tps.engine import ccc
from tps.engine.beam_model import BeamModelParams, FluenceGrid, aperture, energy_fluence
from tps.engine.calc import DoseGrid, Segment
from tps.engine.geometry import beam_frame
from tps.mc import fisica as F
from tps.mc.transporte import lote
from tps.physics.spectrum import Spectrum

MEV_G_PARA_GY = 1.602176634e-10
RHO_MIN = 0.05            # g/cm³: abaixo disso (ar, gás) a dose é a da vizinhança 3×3×3
CORTE_ELETRON = 0.2       # MeV: abaixo disso o elétron deposita no lugar (também o limiar dos δ)
F_PASSO = 0.25            # fração máxima de perda de energia por passo do elétron (0,1 → 0,25: +26% de velocidade)
HISTORIAS_AJUSTE = 20_000_000       # por valor de k no ajuste do espectro
HISTORIAS_CALIBRACAO = 30_000_000   # no 10×10 de referência


@dataclass
class ResultadoMC:
    dose: np.ndarray            # Gy (por UM do conjunto de segmentos, já calibrada)
    incerteza_pct: float        # 1σ médio nos voxels > 50% do máximo
    historias: int
    tempo_s: float
    dose_na_agua: bool


def _espectro(p: BeamModelParams):
    s = Spectrum.mohan(int(p.energy_mv), p.spectrum_hardening_k)
    e = s.energies
    bordas = np.r_[max(e[0] - (e[1] - e[0]) / 2, 0.01), 0.5 * (e[1:] + e[:-1]), e[-1] + (e[-1] - e[-2]) / 2]
    cdf = np.cumsum(s.fluence)
    return cdf / cdf[-1], bordas[:-1].copy(), bordas[1:].copy()


def _fonte(segs: list[Segment], p: BeamModelParams, posicao: str, fg: FluenceGrid, limiar: float = 2e-4):
    """Células do mapa de fluência de cada segmento (cm), pesos (UM × ∫ψ) e geometria."""
    cdfs, us, vs, ofs, ns, pesos, geo = [], [], [], [], [], [], []
    o = 0
    for s in segs:
        ap = aperture(fg, s.jaw_x, s.jaw_y, s.mlc_a, s.mlc_b, s.leaf_boundaries, p)
        psi = energy_fluence(ap, fg, p)
        m = psi > limiar * float(psi.max())
        iv, iu = np.nonzero(m)
        w = psi[iv, iu]
        if w.sum() <= 0 or s.mu <= 0:
            continue
        cdfs.append(np.cumsum(w) / w.sum())
        us.append(fg.u[iu] / 10.0)
        vs.append(fg.v[iv] / 10.0)
        ofs.append(o)
        ns.append(len(w))
        o += len(w)
        pesos.append(float(s.mu) * float(w.sum()) * fg.du * fg.dv / 100.0)     # UM × ∫ψ dA (cm²)
        fr = beam_frame(s.gantry, s.collimator, s.couch, s.isocenter, s.sad, posicao)
        geo.append(np.r_[fr.source / 10.0, fr.u, fr.v, fr.w, fr.sad / 10.0])
    if not pesos:
        return None
    pesos = np.asarray(pesos)
    seg_cdf = np.cumsum(pesos) / pesos.sum()
    return (seg_cdf, np.asarray(ofs, np.int64), np.asarray(ns, np.int64), np.concatenate(cdfs),
            np.concatenate(us), np.concatenate(vs), fg.du / 10.0, fg.dv / 10.0, np.asarray(geo), float(pesos.sum()))


def _contaminacao(grid: DoseGrid, segs: list[Segment], p: BeamModelParams, posicao: str, fg: FluenceGrid):
    """Parcela analítica de contaminação eletrônica do modelo (igual à do CCC), em Gy."""
    if not (p.e_contam_amp > 0 and p.e_contam_ref_raw > 0):
        return None
    spectrum = Spectrum.mohan(int(p.energy_mv), p.spectrum_hardening_k)
    tab, tab_step = ccc.terma_table(spectrum.energies, spectrum.energy_fluence)
    eta = np.ascontiguousarray(grid.eta, np.float64)
    origin = np.asarray(grid.origin_mm, float) / 10.0
    spacing = np.asarray(grid.spacing_mm, float) / 10.0
    econ = np.zeros_like(eta)
    sig_e = float(getattr(p, "e_contam_sigma_mm", 0.0))
    profundidade = {}
    for s in segs:
        fr = beam_frame(s.gantry, s.collimator, s.couch, s.isocenter, s.sad, posicao)
        chave = round(s.gantry / 10.0)
        if chave not in profundidade:
            profundidade[chave] = ccc.radiological_depth(eta, origin, spacing, fr.source / 10.0)
        ap = aperture(fg, s.jaw_x, s.jaw_y, s.mlc_a, s.mlc_b, s.leaf_boundaries, p)
        psi = np.ascontiguousarray(energy_fluence(ap, fg, p) * s.mu)
        if sig_e > 0:
            from scipy.ndimage import gaussian_filter
            psi = np.ascontiguousarray(gaussian_filter(psi, sig_e / fg.du, mode="constant"))
        ccc.terma_hybrid(eta, origin, spacing, fr.source / 10.0, fr.w, fr.u, fr.v, fr.sad / 10.0, psi,
                         fg.u0 / 10.0, fg.v0 / 10.0, fg.du / 10.0, fg.dv / 10.0, tab, tab_step,
                         profundidade[chave], 0.05 * float(psi.max()), p.e_contam_k_cm, np.zeros_like(eta), econ)
    return p.e_contam_amp * p.e_contam_ref_raw * econ * p.norm_dose_per_mu


def materiais(eta: np.ndarray) -> dict:
    m = F.material_de_eta(eta)
    rho = np.maximum(eta * m["rho_por_eta"], 1e-4)
    return {"rho": rho, "f_pares": m["f_pares"], "f_col": m["f_col"], "f_rad": m["f_rad"],
            "x0cm": m["x0"] / rho, "agua_por_meio": m["rho_por_eta"] / m["f_col"]}


def transportar(grid: DoseGrid, segs: list[Segment], p: BeamModelParams, posicao: str = "HFS",
                incerteza_pct: float = 1.0, historias_max: int = 2_000_000_000, historias_min: int = 2_000_000,
                lotes: int = 10, f_passo: float | None = None, semente: int = 12345, progresso=None,
                fluence_grid: FluenceGrid | None = None, dose_na_agua: bool = False,
                tempo_max_s: float | None = None) -> ResultadoMC:
    """Dose (energia depositada → Gy) por unidade de UM×fluência do modelo, SEM a calibração absoluta."""
    t0 = time.time()
    aviso = progresso or (lambda m: None)
    fg = fluence_grid or FluenceGrid()
    fonte = _fonte(segs, p, posicao, fg)
    eta = np.ascontiguousarray(grid.eta, np.float64)
    if fonte is None:
        return ResultadoMC(np.zeros_like(eta), 0.0, 0, 0.0, dose_na_agua)
    seg_cdf, seg_ofs, seg_n, cel_cdf, cel_u, cel_v, du, dv, geo, f_total = fonte
    mat = materiais(eta)
    f_passo = F_PASSO if f_passo is None else f_passo
    tab = F.tabelas(CORTE_ELETRON)
    tabs = np.ascontiguousarray(np.stack([tab.mu_compton, tab.mu_pares, tab.mu_resto, tab.s_col, tab.s_rad,
                                          tab.sigma_delta]))
    esp_cdf, esp_lo, esp_hi = _espectro(p)
    origem = np.asarray(grid.origin_mm, float) / 10.0
    passo = np.asarray(grid.spacing_mm, float) / 10.0
    n3 = np.array([eta.shape[2], eta.shape[1], eta.shape[0]], np.int64)
    lo = origem - passo / 2
    hi = origem + passo * (n3 - 0.5)
    cortes = np.array([CORTE_ELETRON, 0.02, tab.corte_delta])
    nthreads = numba.get_num_threads()
    nblocos = int(max(1, min(nthreads, 1.5e9 // max(eta.size * 4, 1))))      # memória: uma grade float32 por bloco
    edep = np.zeros((nblocos,) + eta.shape, np.float32)
    vol = float(np.prod(passo))
    massa = mat["rho"] * vol
    baixa = mat["rho"] < RHO_MIN
    tecido = ~baixa
    s1 = np.zeros_like(eta)          # Σ n_b·d_b
    s2 = np.zeros_like(eta)          # Σ n_b²·d_b²
    s3 = np.zeros_like(eta)          # Σ n_b²·d_b
    w2 = 0.0                         # Σ n_b²
    n_total = 0
    mu_max_eta = float(eta.max())
    k = 0
    n_lote = max(historias_min // lotes, 1000)
    alvo = incerteza_pct / 100.0
    inc = 1.0
    while True:
        edep[:] = 0.0
        lote(edep, n_lote, semente + 104729 * k, eta, mat["f_pares"], mat["f_col"], mat["f_rad"], mat["x0cm"],
             mu_max_eta, origem, passo, n3, tabs, tab.lne0, tab.dlne, cortes, f_passo, seg_cdf, seg_ofs, seg_n,
             cel_cdf, cel_u, cel_v, du, dv, geo, esp_cdf, esp_lo, esp_hi, lo, hi)
        e_tot = edep.sum(axis=0, dtype=np.float64)
        d = e_tot / massa / n_lote          # MeV/g por história, neste lote
        if baixa.any():                     # gás/ar: energia e massa da vizinhança 3×3×3 (sem picos de massa ~0)
            from scipy.ndimage import uniform_filter
            d[baixa] = np.maximum(uniform_filter(e_tot, 3, mode="nearest") /       # o filtro deixa resíduo < 0
                                  np.maximum(uniform_filter(massa, 3, mode="nearest"), RHO_MIN * vol), 0.0)[baixa] / n_lote
        s1 += n_lote * d
        s2 += (n_lote * d) ** 2
        s3 += n_lote ** 2 * d
        w2 += float(n_lote) ** 2
        n_total += n_lote
        k += 1
        media = s1 / n_total
        if k < 3:
            continue
        var = np.maximum(s2 - 2 * media * s3 + media ** 2 * w2, 0.0) / n_total ** 2 * k / (k - 1)
        alto = tecido & (media > 0.5 * media[tecido].max()) if tecido.any() else media > 0.5 * media.max()
        inc = float(np.mean(np.sqrt(var[alto]) / media[alto])) if alto.any() else 1.0
        aviso(f"Monte Carlo: {n_total / 1e6:.1f} M histórias, incerteza {inc * 100:.2f}%")
        if inc <= alvo or n_total >= historias_max or (tempo_max_s and time.time() - t0 > tempo_max_s):
            break
        falta = n_total * ((inc / alvo) ** 2 - 1)        # incerteza ∝ 1/√N
        n_lote = int(min(max(falta / 3, n_lote), 4 * n_lote, historias_max - n_total))
        n_lote = max(n_lote, 1000)
    dose = media * f_total * MEV_G_PARA_GY
    if dose_na_agua:
        dose = dose * mat["agua_por_meio"]
    return ResultadoMC(dose, inc * 100, n_total, round(time.time() - t0, 1), dose_na_agua)


# ------------------------------------------------------------------ calibração absoluta
def _chave_modelo(p: BeamModelParams) -> str:
    d = {k: v for k, v in asdict(p).items() if k not in ("comissionado", "demonstracao")}
    d["_transporte"] = (CORTE_ELETRON, F_PASSO)                # mudar o transporte refaz a calibração
    return hashlib.sha256(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _cax(d, meia_largura: int = 2):
    """Dose média no eixo central (2·meia_largura voxels de lado), em função da profundidade."""
    nz, ny, nx = d.shape
    h = meia_largura
    return d[nz // 2 - h:nz // 2 + h, :, nx // 2 - h:nx // 2 + h].mean(axis=(0, 2))


def ajustar_espectro(p: BeamModelParams, cones, posicao: str = "HFS", leaf_boundaries=None, progresso=None,
                     historias: int | None = None) -> float:
    """Endurecimento do espectro do MC (k, g/cm² de água) que reproduz o PDD do 10×10 do modelo comissionado
    de 3 a 25 cm. O k do CCC é um parâmetro efetivo (compensa as aproximações do kernel e vem junto com a escala do
    espalhamento) e não serve a um transporte físico. Provisório: com os PDDs medidos o ajuste é feito na medida."""
    import copy
    from tps.engine.calc import compute_dose
    from tps.engine.phantom import square_field, water_box
    historias = historias or HISTORIAS_AJUSTE
    g = water_box(300.0, 300.0, 5.0)
    seg = square_field(10.0, 10.0, 1000.0, 1000.0, 1.0, leaf_boundaries)
    a = _cax(compute_dose(g, [seg], p, cones), 4)          # média em 4×4 cm: menos ruído no MC
    econ = _contaminacao(g, [seg], p, posicao, FluenceGrid())
    if econ is not None:
        a = a - _cax(econ, 4)
    y = g.origin_mm[1] + np.arange(g.eta.shape[1]) * g.spacing_mm[1]
    sel = (y >= 50) & (y <= 250)                           # sem contaminação eletrônica
    ks, erros = [], []
    for k in (1.0, 5.0, 9.0):
        if progresso:
            progresso(f"Monte Carlo: ajustando o espectro ao PDD do modelo (k = {k:g})…")
        q = copy.copy(p)
        q.spectrum_hardening_k = k
        r = transportar(g, [seg], q, posicao, incerteza_pct=0.5, historias_max=historias, historias_min=historias // 2)
        b = _cax(r.dose, 4)
        # razão MC/CCC normalizada pela média da faixa (não por um ponto só): menos ruído
        rel = (b[sel] / a[sel]) / np.mean(b[sel] / a[sel]) - 1
        prof = y[sel]
        rel = float(np.polyfit(prof - prof.mean(), rel, 1)[0]) * 100.0   # inclinação (fração por 10 cm)
        ks.append(k)
        erros.append(rel)                                  # >0: o MC penetra demais
    ks, erros = np.asarray(ks), np.asarray(erros)
    c = np.polyfit(ks, erros, 1)                           # o erro médio é quase linear em k
    return float(np.clip(-c[1] / c[0], -5.0, 20.0)) if abs(c[0]) > 1e-9 else 5.0


def fator_calibracao(p: BeamModelParams, cones, posicao: str = "HFS", leaf_boundaries=None,
                     progresso=None, cache: dict | None = None) -> float:
    """Fator Gy/(unidade do MC) para que o MC dê a mesma dose do modelo comissionado num 10×10, DFS 100, no dmax.
    Guardado em <home>/mc/calibracao.json por modelo de feixe, junto com o espectro do MC."""
    return _calibracao(p, cones, posicao, leaf_boundaries, progresso)["fator"]


def espectro_mc(p: BeamModelParams, cones, posicao: str = "HFS", leaf_boundaries=None, progresso=None) -> float:
    return _calibracao(p, cones, posicao, leaf_boundaries, progresso)["k_espectro"]


def _calibracao(p: BeamModelParams, cones, posicao: str = "HFS", leaf_boundaries=None,
                progresso=None) -> dict:
    from tps import paths
    from tps.engine.calc import compute_dose
    from tps.engine.phantom import square_field, water_box
    chave = _chave_modelo(p)
    arq = paths.home() / "mc" / "calibracao.json"
    banco = {}
    if arq.exists():
        try:
            banco = json.loads(arq.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            banco = {}
    if chave in banco and "escala_contaminacao" in banco[chave]:
        return banco[chave]
    import copy
    k_esp = getattr(p, "mc_spectrum_hardening_k", None)
    if k_esp is None:
        k_esp = ajustar_espectro(p, cones, posicao, leaf_boundaries, progresso)
    q = copy.copy(p)
    q.spectrum_hardening_k = k_esp
    if progresso:
        progresso("Monte Carlo: calibrando no 10×10 em água (uma vez por modelo de feixe)…")
    g = water_box(300.0, 250.0, 5.0)
    seg = square_field(10.0, 10.0, 1000.0, 1000.0, 1.0, leaf_boundaries)
    d_ccc = compute_dose(g, [seg], p, cones)
    r = transportar(g, [seg], q, posicao, incerteza_pct=0.3, historias_max=HISTORIAS_CALIBRACAO,
                    historias_min=HISTORIAS_CALIBRACAO // 3, progresso=progresso)
    econ = _contaminacao(g, [seg], p, posicao, FluenceGrid())
    y = g.origin_mm[1] + np.arange(g.eta.shape[1]) * g.spacing_mm[1]
    dc, dm = _cax(d_ccc, 4), _cax(r.dose, 4)             # 4×4 cm no centro do 10×10 (dose plana)
    de = _cax(econ, 4) if econ is not None else np.zeros_like(dc)
    i = int(np.argmax(dc))
    # fator absoluto e amplitude da contaminação do MC: mínimos quadrados (erro relativo) no PDD de 2,5 a 250 mm,
    # com peso maior em torno do dmax (o ponto de calibração)
    ok = (y >= 2.5) & (y <= 250)
    wgt = 1.0 / np.maximum(dc[ok], 1e-30) * (1 + 4 * np.exp(-((y[ok] - y[i]) / 10.0) ** 2))
    if de.max() > 0:
        Aq = np.column_stack([dm[ok], de[ok]]) * wgt[:, None]
        (fator, a_e), *_ = np.linalg.lstsq(Aq, dc[ok] * wgt, rcond=None)
        a_e = float(max(a_e, 0.0))
    else:
        fator = float(np.sum(dm[ok] * dc[ok] * wgt ** 2) / np.sum((dm[ok] * wgt) ** 2))
        a_e = 0.0
    fator = float(fator)
    banco[chave] = {"fator": fator, "k_espectro": float(k_esp), "escala_contaminacao": a_e, "dmax_mm": float(y[i]),
                    "incerteza_pct": r.incerteza_pct, "historias": r.historias,
                    "quando": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "origem": "medida" if getattr(p, "mc_spectrum_hardening_k", None) is not None else
                    "ajuste ao modelo CCC (provisório)"}
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(banco, indent=1), encoding="utf-8")
    return banco[chave]


def dose_mc(grid: DoseGrid, segs: list[Segment], p: BeamModelParams, cones, posicao: str = "HFS",
            incerteza_pct: float = 1.0, dose_na_agua: bool = False, progresso=None, **kw) -> ResultadoMC:
    """Dose absoluta (Gy) do MC: transporte × calibração + contaminação eletrônica do modelo."""
    import copy
    lb = next((s.leaf_boundaries for s in segs if s.leaf_boundaries is not None), None)
    cal = _calibracao(p, cones, posicao, lb, progresso)
    q = copy.copy(p)
    q.spectrum_hardening_k = cal["k_espectro"]
    r = transportar(grid, segs, q, posicao, incerteza_pct, dose_na_agua=dose_na_agua, progresso=progresso, **kw)
    r.dose = r.dose * cal["fator"]
    econ = _contaminacao(grid, segs, p, posicao, kw.get("fluence_grid") or FluenceGrid())
    if econ is not None:
        r.dose = r.dose + cal.get("escala_contaminacao", 1.0) * econ
    return r
