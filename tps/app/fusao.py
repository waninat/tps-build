"""Fusão CT–CT na sessão: carregar a TC secundária, registrar (rígido/deformável), ver a fusão e propagar
estruturas. SRS-IMG-040. As funções recebem a Sessao (ficam fora de sessao.py para não crescer mais o arquivo)."""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pydicom
from PIL import Image

from tps.imaging import registro as R


class ErroFusao(ValueError):
    pass


def _grade_ct(ct) -> R.Grade:
    x, y, z = ct.axes()
    return R.Grade(np.asarray(x, float), np.asarray(y, float), np.asarray(z, float))


def carregar(s, pasta: str | Path) -> dict:
    from tps.dicom_io.rtstruct import read_rtstruct
    from tps.imaging.ct import read_ct_series
    pasta = Path(pasta)
    if s.ct is None or s.tipo != "ct":
        raise ErroFusao("Abra primeiro a TC principal (a do plano)")
    if not pasta.is_dir():
        raise ErroFusao(f"Pasta não encontrada: {pasta}")
    try:
        ct = read_ct_series(pasta)
    except (ValueError, NotImplementedError) as e:
        raise ErroFusao(str(e)) from e
    if (ct.patient_position or "HFS") != "HFS":
        raise ErroFusao(f"TC secundária em {ct.patient_position}: esta versão só aceita HFS")
    ests = []
    for f in sorted(p for p in pasta.iterdir() if p.is_file()):
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
        except Exception:  # noqa: BLE001
            continue
        if getattr(ds, "Modality", "") == "RTSTRUCT":
            try:
                ests = read_rtstruct(f)
            except Exception:  # noqa: BLE001
                ests = []
            break
    mesmo = ct.frame_of_reference and ct.frame_of_reference == s.ct.frame_of_reference
    s.secundaria = {"ct": ct, "pasta": str(pasta), "estruturas": ests, "reg": None, "mesmo_referencial": bool(mesmo)}
    s._registrar(f"TC secundária carregada ({pasta.name}; {len(ests)} estruturas)")
    return estado(s)


def estado(s) -> dict:
    sec = getattr(s, "secundaria", None)
    if not sec:
        return {"carregada": False}
    ct = sec["ct"]
    reg = sec.get("reg")
    return {"carregada": True, "pasta": sec["pasta"], "cortes": int(ct.hu.shape[0]),
            "voxel_mm": [round(float(v), 2) for v in ct.spacing], "descricao": ct.description,
            "mesmo_referencial": sec["mesmo_referencial"],
            "estruturas": [e.name for e in sec["estruturas"]], "registro": reg.resumo() if reg else None,
            "qualidade": sec.get("qualidade")}


def registrar(s, modo: str = "deformavel", rigido_inicial: bool = True, niveis_mm=None, iteracoes=None,
              sigma_campo_mm: float = 4.0, progresso=None) -> dict:
    sec = getattr(s, "secundaria", None)
    if not sec:
        raise ErroFusao("Carregue a TC secundária primeiro")
    if modo not in ("rigido", "deformavel", "identidade"):
        raise ErroFusao("Modo do registro: rigido, deformavel ou identidade")
    gf, gm = _grade_ct(s.ct), _grade_ct(sec["ct"])
    corpo = s._corpo_ct()
    q0 = R.qualidade(s.ct.hu, gf, sec["ct"].hu, gm, R.Registro(), corpo)
    if modo == "identidade":
        reg = R.Registro()
    else:
        reg = R.rigido(s.ct.hu, gf, sec["ct"].hu, gm, corpo, progresso=progresso) if rigido_inicial else R.Registro()
        if modo == "deformavel":
            kw = {}
            if niveis_mm:
                kw["niveis_mm"] = tuple(float(v) for v in niveis_mm)
            if iteracoes:
                kw["iteracoes"] = tuple(int(v) for v in iteracoes)
            reg = R.deformavel(s.ct.hu, gf, sec["ct"].hu, gm, reg, corpo, sigma_campo_mm=sigma_campo_mm,
                               progresso=progresso, **kw)
    q1 = R.qualidade(s.ct.hu, gf, sec["ct"].hu, gm, reg, corpo)
    sec["reg"] = reg
    sec["qualidade"] = {"antes": q0, "depois": q1}
    avisos = []
    if reg.deformavel and reg.metricas.get("dobras_pct", 0) > 0.1:
        avisos.append(f"Campo com dobras em {reg.metricas['dobras_pct']}% do corpo: aumente a regularização")
    if reg.deformavel and reg.metricas.get("desloc_max_mm", 0) > 30:
        avisos.append(f"Deslocamento máximo {reg.metricas['desloc_max_mm']} mm: confira a fusão no xadrez")
    if q1["ncc"] < q0["ncc"] - 0.01:
        avisos.append("O registro piorou a correlação: confira as TCs (são do mesmo paciente/região?)")
    s._registrar(f"Registro {modo}: NCC {q0['ncc']} → {q1['ncc']}, erro médio {q0['mae_hu']} → {q1['mae_hu']} HU")
    return {**estado(s), "avisos": avisos}


def _pontos_corte(s, vista: str, idx: int):
    cx, cy, cz = (np.asarray(a, float) for a in s.ct.axes())
    if vista == "axial":
        Y, X = np.meshgrid(cy, cx, indexing="ij")
        return np.column_stack([X.ravel(), Y.ravel(), np.full(X.size, cz[idx])]), (len(cy), len(cx))
    zz = cz[::-1]
    if vista == "coronal":
        Z, X = np.meshgrid(zz, cx, indexing="ij")
        return np.column_stack([X.ravel(), np.full(X.size, cy[idx]), Z.ravel()]), (len(zz), len(cx))
    Z, Y = np.meshgrid(zz, cy, indexing="ij")
    return np.column_stack([np.full(Y.size, cx[idx]), Y.ravel(), Z.ravel()]), (len(zz), len(cy))


def corte_secundaria(s, vista: str, idx: int) -> np.ndarray:
    sec = getattr(s, "secundaria", None)
    if not sec:
        raise ErroFusao("Sem TC secundária")
    reg = sec.get("reg") or R.Registro()
    pts, forma = _pontos_corte(s, vista, int(idx))
    return R.amostrar(sec["ct"].hu, _grade_ct(sec["ct"]), reg.aplicar(pts), 1, -1000.0).reshape(forma)


def imagem(s, vista: str, idx: int, modo: str = "mistura", alfa: float = 0.5, janela: float = 400,
           nivel: float = 40, xadrez_px: int = 32) -> bytes:
    """PNG do corte com a fusão: mistura (principal em cinza + secundária em magenta/verde), xadrez, diferença ou
    só a secundária (já registrada)."""
    a = s._fatia(vista, int(idx)).astype(np.float32)
    b = corte_secundaria(s, vista, idx)
    jan = lambda v: np.clip((v - (nivel - janela / 2)) / janela, 0, 1)   # noqa: E731
    A, B = jan(a), jan(b)
    if modo == "secundaria":
        rgb = np.stack([B, B, B], -1)
    elif modo == "xadrez":
        iy, ix = np.indices(A.shape)
        sel = ((iy // xadrez_px + ix // xadrez_px) % 2).astype(bool)
        g = np.where(sel, B, A)
        rgb = np.stack([g, g, g], -1)
    elif modo == "diferenca":                                # vermelho: secundária mais densa; azul: menos
        d = np.clip((b - a) / 400.0, -1, 1)
        pos, neg = np.clip(d, 0, 1), np.clip(-d, 0, 1)
        rgb = np.clip(np.stack([0.5 + 0.5 * pos - 0.5 * neg, 0.5 - 0.5 * np.abs(d), 0.5 + 0.5 * neg - 0.5 * pos], -1), 0, 1)
    else:                                                     # mistura: principal em verde, secundária em magenta
        al = float(np.clip(alfa, 0, 1))
        wa, wb = min(1.0, 2 * (1 - al)), min(1.0, 2 * al)
        rgb = np.clip(np.stack([wb * B, wa * A, wb * B], -1), 0, 1)
    buf = io.BytesIO()
    Image.fromarray((rgb * 255).astype(np.uint8)).save(buf, "PNG", optimize=False, compress_level=1)
    return buf.getvalue()


def propagar(s, nomes: list[str] | None = None, sufixo: str = "_def", suavizar_mm: float = 1.5) -> dict:
    """Estruturas da TC secundária levadas para a principal pela transformação do registro."""
    from tps.planejamento import contorno as K
    sec = getattr(s, "secundaria", None)
    if not sec or not sec.get("reg"):
        raise ErroFusao("Registre a TC secundária antes de propagar")
    reg = sec["reg"]
    ct_m = sec["ct"]
    gm = _grade_ct(ct_m)
    mx, my, mz = gm.x, gm.y, gm.z
    cx, cy, cz = (np.asarray(v, float) for v in s.ct.axes())
    pedidas = [e for e in sec["estruturas"] if not nomes or e.name in nomes]
    if not pedidas:
        raise ErroFusao("Nenhuma estrutura da TC secundária para propagar")
    margem = 15.0 + float(reg.metricas.get("desloc_max_mm", 0.0) or 0.0)
    feitas, avisos = [], []
    Rm, c, t = reg.matriz(), np.asarray(reg.centro_mm), np.asarray(reg.translacao_mm)
    tol = float(abs(mz[1] - mz[0])) / 2 + 0.01 if len(mz) > 1 else 1.5
    for e in pedidas:
        if not e.contours:
            continue
        # máscara na TC secundária, só na caixa da estrutura
        todos = np.vstack(e.contours)
        lo, hi = todos.min(axis=0) - 6, todos.max(axis=0) + 6
        sx = (mx >= lo[0]) & (mx <= hi[0])
        sy = (my >= lo[1]) & (my <= hi[1])
        sz = (mz >= lo[2] - tol) & (mz <= hi[2] + tol)
        if not (sx.any() and sy.any() and sz.any()):
            continue
        bx, by, bz = mx[sx], my[sy], mz[sz]
        vol = np.stack([K.mascara_de_poligonos(K.poligonos_do_corte(e.contours, float(z), tol), bx, by) for z in bz])
        if not vol.any():
            continue
        g_box = R.Grade(bx, by, bz) if len(bz) > 1 else R.Grade(bx, by, np.r_[bz, bz + 1.0])
        # caixa na principal: cantos da caixa levados pela inversa do rígido, com folga pelo deformável
        cantos = np.array([[a, b_, z] for a in (lo[0], hi[0]) for b_ in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        inv = (cantos - c - t) @ Rm + c
        plo, phi = inv.min(axis=0) - margem, inv.max(axis=0) + margem
        kx = (cx >= plo[0]) & (cx <= phi[0])
        ky = (cy >= plo[1]) & (cy <= phi[1])
        kz = (cz >= plo[2]) & (cz <= phi[2])
        if not (kx.any() and ky.any() and kz.any()):
            avisos.append(f"{e.name}: fora da TC principal")
            continue
        dest = R.Grade(cx[kx], cy[ky], cz[kz])
        m = R.propagar_mascara(vol, g_box, dest, reg)
        if not m.any():
            avisos.append(f"{e.name}: ficou vazia na TC principal")
            continue
        nome = (e.name[:32 - len(sufixo)] + sufixo)
        base, k = nome, 2
        while nome in s.estruturas:
            nome = f"{base[:29]}_{k}"
            k += 1
        tipo = (e.rt_type or "ORGAN").upper()
        s.nova_estrutura(nome, tipo if tipo in ("PTV", "CTV", "GTV", "ORGAN", "AVOIDANCE") else "ORGAN", e.color)
        est = s.estruturas[nome]
        polys = []
        if suavizar_mm > 0:
            from tps.planejamento import suavizacao as S
            passo = dest.passo
            campo, nivel = S.campo_suave(m, passo, suavizar_mm)
            for kk, z in enumerate(dest.z):
                polys += S.poligonos(campo[kk], nivel, dest.x, dest.y, float(z))
        else:
            for kk, z in enumerate(dest.z):
                if m[kk].any():
                    polys += K.poligonos_de_mascara(m[kk], dest.x, dest.y, float(z), suavizar=0.4, min_pontos=6)
        est.contornos = polys
        s._depois_de_editar(est)
        v0 = float(vol.sum() * np.prod(g_box.passo) / 1000.0)
        v1 = float(est.mascara.sum() * s.grade.voxel_cc) if est.mascara is not None else 0.0
        feitas.append({"origem": e.name, "nome": nome, "volume_secundaria_cc": round(v0, 1),
                       "volume_principal_cc": round(v1, 1)})
    s._registrar(f"Estruturas propagadas da TC secundária ({'deformável' if reg.deformavel else 'rígido'}): "
                 + ", ".join(f["nome"] for f in feitas))
    return {"propagadas": feitas, "avisos": avisos}
