"""Fluxo automático: TC chega → contornos dos órgãos → (médico desenha e aprova os alvos) → plano pronto para o físico.
SRS-AUT-001..010.

Configurável em Ferramentas › Automação (arquivo <dados>/config/automacao.yaml); tudo desligado por padrão.

1. **Recepção.** O TPS olha uma pasta (onde o tomógrafo/Mosaiq/PACS grava, ou o receptor DICOM do próprio TPS) e,
   quando uma série de TC termina de chegar (sem arquivos novos por `estabilizar_s`), copia para um CASO em
   <dados>/casos/<id>/ — os arquivos originais não mudam.
2. **Contorno automático.** BODY pelo limiar de HU; identifica o sítio (descrição do exame, sexo, anatomia: pulmão,
   crânio, cabeças femorais) e contorna por IA (TotalSegmentator, se instalado) os órgãos do sítio; sem IA, os
   pulmões por limiar. Grava RS_auto_*.dcm no caso (RTSTRUCT com tudo) — pronto para o médico desenhar os alvos.
3. **Alvos e aprovação.** O médico desenha os PTVs com a prescrição no nome (PTVPelve45@25, PTV_Mama_50@25,
   PTV70@35…) — no TPS ou em outro sistema (o RTSTRUCT que chegar na pasta de entrada com o mesmo referencial entra
   no caso). O gatilho do plano é configurável: a APROVAÇÃO das estruturas (no TPS, com senha de médico/físico, ou o
   RTSTRUCT recebido já aprovado — ApprovalStatus APPROVED) ou apenas a presença de PTVs com prescrição no nome.
4. **Plano automático.** Um plano por grupo de PTVs com o mesmo número de frações (boost integrado = um plano com
   vários alvos); o nome do plano é o nome do PTV de maior dose (até 16 caracteres). Técnica por sítio
   (3D pelo modelo do sítio, IMRT ou VMAT com objetivos do assistente — Claude, se configurado, ou regras). O plano é
   calculado, salvo e fica "aguardando o físico"; com o Claude, sai também o parecer (sem alterar nada).

Nada é aprovado nem exportado automaticamente.
"""
from __future__ import annotations

import datetime as dt
import queue
import re
import shutil
import threading
import time
import traceback
from pathlib import Path

import pydicom
import yaml

from tps import paths

PADRAO = {
    "ativo": False,
    "pasta_entrada": "",
    "intervalo_s": 30,
    "estabilizar_s": 60,
    "receptor_dicom": {"ativo": False, "porta": 11112, "ae": "TPS"},
    "contorno": {"ativo": True, "ia": True, "suavizar_mm": 2.0},
    "plano": {"ativo": False, "gatilho": "aprovacao",          # aprovacao | ptv
              "maquina": "radioserra_precise", "energia": "6MV",
              "tecnica_padrao": "VMAT",
              "tecnica_por_sitio": {"mama": "3D", "encefalo": "3D"},
              "variantes": ["3D", "VMAT"],       # um plano de cada técnica para o físico escolher ([] = por sítio)
              "tentativas": 3,                   # 1ª simples; as seguintes corrigem o que faltou (mais agressivas)
              "motor": "auto", "perfil": "tps", "parecer_claude": True},
}

# palavra no nome do PTV / na descrição do exame → sítio (protocolo do assistente)
SITIOS = [
    (r"\bcp\b|c\.p\.|cabe[cç]a\s*(e|&)?\s*pesco[cç]o", "cabeca_pescoco"), (r"\bsnc\b|\bcr[aâ]nio\b|\btc\s*cr", "encefalo"),
    (r"\bmd\b|\bme\b(?!d)|\bmama", "mama"), (r"\bpel\b|\bpelv", "pelve"),
    (r"prost", "prostata"), (r"mama|breast|parede", "mama"), (r"pulm|lung", "pulmao"), (r"esof", "esofago"),
    (r"enc[eé]f|cerebr|cranio|crânio|brain|glio", "encefalo"), (r"cab|pesc|orof|laring|nasof|boca|h&n|hn", "cabeca_pescoco"),
    (r"colo|utero|útero|endom|gine|cervix|colodo", "pelve_ginecologica"), (r"reto|anal|anus|rectum", "reto"),
    (r"pelv", "pelve"), (r"t[oó]rax|thorax|chest|mediast", "torax"), (r"abd", "abdome"),
]
MODELO_3D = {"prostata": "prostata", "pulmao": "pulmao", "esofago": "esofago", "encefalo": "encefalo_total",
             "pelve_ginecologica": "colo_uterino", "reto": "reto", "pelve": "colo_uterino", "torax": "pulmao",
             "abdome": "prostata", "cabeca_pescoco": "encefalo_total"}   # mama: mama_d / mama_e pelo lado
# PTV de drenagem (fossa supraclavicular, axila, cadeias): campos diretos AP (+PA) na mama
RE_FSC = re.compile(r"fsc|scf|supra|clav|fossa|axil|nodal|linfo|cadeia|\bln\b|imn|mam.*int", re.IGNORECASE)
ORGAOS_SITIO = {
    "pelve": ["bexiga", "reto", "bowel_bag", "femur_e", "femur_d", "colon", "intestino_delgado"],
    "torax": ["coracao", "pulmao_e", "pulmao_d", "esofago", "medula", "traqueia"],
    "mama": ["coracao", "pulmao_e", "pulmao_d", "medula", "mama_e", "mama_d", "esofago"],
    "abdome": ["figado", "estomago", "rim_e", "rim_d", "medula", "intestino_delgado", "colon"],
    "cabeca_pescoco": ["parotida_e", "parotida_d", "submandibular_e", "submandibular_d", "cavidade_oral",
                       "constritores", "mandibula", "medula", "tireoide", "olho_e", "olho_d", "cristalino_e",
                       "cristalino_d", "nervo_optico_e", "nervo_optico_d"],
    "encefalo": ["encefalo", "olho_e", "olho_d", "cristalino_e", "cristalino_d", "nervo_optico_e", "nervo_optico_d"],
}
RE_PTV = re.compile(r"^(PTV|ptv)[\s_\-]*([A-Za-zÀ-ÿ_\-\s]*?)[\s_\-]*(\d+(?:[.,]\d+)?)\s*(c?gy)?\s*@\s*(\d{1,2})\s*(fx)?$",
                    re.IGNORECASE)

_trava = threading.RLock()
_estado = {"rodando": False, "fila": 0, "atual": None, "ultimo_erro": None, "log": []}
_fila: "queue.Queue[tuple]" = queue.Queue()
_threads: list[threading.Thread] = []
_parar = threading.Event()
_geracao = [0]
_receptor = None
_vistos: dict = {}          # caminho → (mtime, tamanho) já lido


# ------------------------------------------------------------------------------------------------ configuração
def _arq_config() -> Path:
    return paths.home() / "config" / "automacao.yaml"


def _mesclar(base: dict, novo: dict) -> dict:
    out = dict(base)
    for k, v in (novo or {}).items():
        out[k] = _mesclar(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def config() -> dict:
    f = _arq_config()
    d = {}
    if f.exists():
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            d = {}
    c = _mesclar(PADRAO, d)
    if isinstance((d.get("plano") or {}).get("tecnica_por_sitio"), dict):   # a lista da clínica substitui a padrão
        c["plano"]["tecnica_por_sitio"] = dict(d["plano"]["tecnica_por_sitio"])
    return c


def salvar_config(novo: dict) -> dict:
    c = _mesclar(config(), novo or {})
    if isinstance((novo or {}).get("plano", {}).get("tecnica_por_sitio"), dict):
        c["plano"]["tecnica_por_sitio"] = dict(novo["plano"]["tecnica_por_sitio"])
    if c["plano"]["gatilho"] not in ("aprovacao", "ptv"):
        raise ValueError("Gatilho do plano: aprovacao ou ptv")
    for t in [c["plano"]["tecnica_padrao"], *c["plano"]["tecnica_por_sitio"].values()]:
        if t not in ("3D", "IMRT", "VMAT"):
            raise ValueError(f"Técnica {t}: use 3D, IMRT ou VMAT")
    var = c["plano"].get("variantes") or []
    if not isinstance(var, list) or any(t not in ("3D", "IMRT", "VMAT") for t in var):
        raise ValueError("Variantes do plano: lista com 3D, IMRT e/ou VMAT")
    c["plano"]["variantes"] = list(dict.fromkeys(var))
    c["plano"]["tentativas"] = max(1, min(3, int(c["plano"].get("tentativas") or 3)))
    if c["ativo"] and not c["pasta_entrada"] and not c["receptor_dicom"]["ativo"]:
        raise ValueError("Informe a pasta de entrada (ou ligue o receptor DICOM)")
    if c["pasta_entrada"]:
        Path(c["pasta_entrada"]).mkdir(parents=True, exist_ok=True)
    f = _arq_config()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(c, allow_unicode=True, sort_keys=False), encoding="utf-8")
    parar()
    if c["ativo"]:
        iniciar()
    return estado()


def _log(msg: str):
    linha = f"{dt.datetime.now():%d/%m %H:%M:%S} {msg}"
    with _trava:
        _estado["log"] = (_estado["log"] + [linha])[-200:]
    try:
        from tps.diagnostico import registrar
        registrar(f"[automação] {msg}")
    except Exception:  # noqa: BLE001
        pass


def estado() -> dict:
    with _trava:
        return {**{k: v for k, v in _estado.items() if k != "log"}, "fila": _fila.qsize(),
                "log": list(_estado["log"][-40:]), "config": config(),
                "receptor": bool(_receptor is not None)}


# ------------------------------------------------------------------------------------------------ nomes e sítio
def prescricao_do_nome(nome: str) -> dict | None:
    """PTVPelve45@25 → {sitio_txt: 'Pelve', dose_gy: 45, fracoes: 25}. Dose ≥ 100 = cGy."""
    m = RE_PTV.match((nome or "").strip())
    if not m:
        return None
    d = float(m.group(3).replace(",", "."))
    if (m.group(4) or "").lower() == "cgy" or d >= 100:
        d /= 100.0
    fx = int(m.group(5))
    if not (1 <= fx <= 50 and 1.0 <= d <= 90.0):
        return None
    return {"nome": nome, "sitio_txt": (m.group(2) or "").strip(" _-"), "dose_gy": round(d, 3), "fracoes": fx}


def sitio_do_texto(txt: str) -> str | None:
    t = (txt or "").lower()
    for padrao, sitio in SITIOS:
        if re.search(padrao, t):
            return sitio
    return None


def identificar_sitio(s) -> dict:
    """Sítio pelo cabeçalho do exame (descrição, parte do corpo) ou pela anatomia da TC."""
    import numpy as np
    ds = pydicom.dcmread(str(next(f for f in sorted(s.pasta_ct.iterdir()) if f.is_file() and s._e_ct(f))),
                         stop_before_pixels=True, force=True)
    sexo = str(getattr(ds, "PatientSex", "") or "").upper()[:1]
    texto = " ".join(str(getattr(ds, k, "") or "") for k in ("StudyDescription", "SeriesDescription", "BodyPartExamined",
                                                             "ProtocolName", "RequestedProcedureDescription"))
    sit = sitio_do_texto(texto)
    if sit:
        return {"sitio": sit, "sexo": sexo, "como": f"descrição do exame: {texto.strip()[:80]}"}
    hu = s.ct.hu
    corpo = s._corpo_ct()
    nz = hu.shape[0]
    area = corpo.reshape(nz, -1).sum(axis=1).astype(float)
    tem = area > 0
    ar = ((hu < -500) & corpo).reshape(nz, -1).sum(axis=1)[tem] / area[tem]
    pulm = float(np.mean(ar > 0.15)) if tem.any() else 0.0   # fração dos cortes com bastante ar dentro do corpo
    area_cm2 = float(np.median(area[tem])) * float(s.ct.spacing[0] * s.ct.spacing[1]) / 100.0 if tem.any() else 0.0
    if pulm > 0.35:
        sit = "torax"
    elif area_cm2 < 350:
        sit = "cabeca_pescoco"                            # secção pequena: cabeça/pescoço (ou encéfalo)
    else:
        sit = "pelve" if pulm < 0.05 else "abdome"
    return {"sitio": sit, "sexo": sexo, "como": f"anatomia (cortes com pulmão {100 * pulm:.0f}%, secção {area_cm2:.0f} cm²)"}


def _orgaos(sitio: str, sexo: str) -> list[str]:
    base = {"prostata": "pelve", "reto": "pelve", "pelve_ginecologica": "pelve", "pulmao": "torax",
            "esofago": "torax"}.get(sitio, sitio)
    org = list(ORGAOS_SITIO.get(base, ORGAOS_SITIO["pelve"]))
    if base == "pelve" and sexo != "F":
        org.append("prostata")
    if base in ("torax", "mama") and sexo == "M":
        org = [o for o in org if not o.startswith("mama")]
    return org


# ------------------------------------------------------------------------------------------------ casos
def _pasta_casos() -> Path:
    p = paths.home() / "casos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ler_caso(cid: str) -> dict:
    f = _pasta_casos() / cid / "caso.yaml"
    return yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else {}


def _gravar_caso(cid: str, **mud) -> dict:
    with _trava:
        d = _ler_caso(cid)
        ev = mud.pop("evento", None)
        d.update(mud)
        if ev:
            d.setdefault("eventos", []).append({"quando": dt.datetime.now().isoformat(timespec="seconds"), "evento": ev})
        d["atualizado"] = dt.datetime.now().isoformat(timespec="seconds")
        f = _pasta_casos() / cid / "caso.yaml"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return d


def casos() -> list[dict]:
    out = []
    for f in sorted(_pasta_casos().glob("*/caso.yaml")):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            continue
        d["id"] = f.parent.name
        d["pasta"] = str(f.parent)
        d.pop("eventos", None)
        out.append(d)
    return sorted(out, key=lambda d: d.get("atualizado", ""), reverse=True)


def caso(cid: str) -> dict:
    d = _ler_caso(cid)
    if not d:
        raise ValueError(f"Caso {cid} não existe")
    return {**d, "id": cid, "pasta": str(_pasta_casos() / cid)}


def caso_da_pasta(pasta) -> str | None:
    try:
        p = Path(pasta).resolve()
        if p.parent == _pasta_casos().resolve() and (p / "caso.yaml").exists():
            return p.name
    except OSError:
        pass
    return None


def _id_caso(ds) -> str:
    base = f"{getattr(ds, 'PatientID', '') or 'SEMID'}_{getattr(ds, 'StudyDate', '') or dt.date.today():}"
    base = re.sub(r"[^A-Za-z0-9_\-]+", "_", str(base))[:40]
    cid, k = base, 2
    while (_pasta_casos() / cid).exists():
        cid = f"{base}_{k}"
        k += 1
    return cid


def _indice() -> dict:
    f = _pasta_casos() / "indice.yaml"
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}


def _gravar_indice(d: dict):
    (_pasta_casos() / "indice.yaml").write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")


def receber_serie(arquivos: list[Path], ds) -> str:
    """Copia uma série de TC para um caso novo e põe o contorno na fila."""
    with _trava:
        idx = _indice()
        serie = str(ds.SeriesInstanceUID)
        if serie in idx.get("series", {}):
            return idx["series"][serie]
        cid = _id_caso(ds)
        pasta = _pasta_casos() / cid
        pasta.mkdir(parents=True)
        for f in arquivos:
            shutil.copy2(f, pasta / f.name)
        idx.setdefault("series", {})[serie] = cid
        idx.setdefault("referenciais", {})[str(getattr(ds, "FrameOfReferenceUID", ""))] = cid
        idx.setdefault("estudos", {})[str(getattr(ds, "StudyInstanceUID", ""))] = cid
        _gravar_indice(idx)
    _gravar_caso(cid, paciente_id=str(getattr(ds, "PatientID", "")), paciente=str(getattr(ds, "PatientName", "")),
                 exame=str(getattr(ds, "StudyDescription", "") or ""), cortes=len(arquivos), estado="recebido",
                 evento=f"TC recebida ({len(arquivos)} cortes)")
    _log(f"TC recebida: caso {cid} ({len(arquivos)} cortes)")
    if config()["contorno"]["ativo"]:
        _fila.put(("contornar", cid))
    return cid


def receber_rtstruct(arquivo: Path, ds) -> str | None:
    """RTSTRUCT de um caso já recebido (mesmo referencial): entra no caso e pode disparar o plano."""
    ref = getattr(ds, "ReferencedFrameOfReferenceSequence", None)
    fr = str(ref[0].get("FrameOfReferenceUID", "")) if ref else ""
    idx = _indice()
    cid = idx.get("referenciais", {}).get(fr) if fr else None
    if not cid and not fr:                               # sem referencial declarado: pelo estudo
        cid = idx.get("estudos", {}).get(str(getattr(ds, "StudyInstanceUID", "")))
    if not cid:
        return None
    destino = _pasta_casos() / cid / f"RS_recebido_{dt.datetime.now():%Y%m%d_%H%M%S}.dcm"
    shutil.copy(arquivo, destino)                         # hora de chegada = hora do arquivo (o mais novo vale)
    aprovado = str(getattr(ds, "ApprovalStatus", "") or "").upper() == "APPROVED"
    nomes = [str(r.ROIName) for r in getattr(ds, "StructureSetROISequence", [])]
    ptvs = [p for p in (prescricao_do_nome(n) for n in nomes) if p]
    _gravar_caso(cid, ptvs=[p["nome"] for p in ptvs],
                 evento=f"RTSTRUCT recebido ({len(nomes)} estruturas, {len(ptvs)} PTV com prescrição"
                        + (", aprovado" if aprovado else "") + ")")
    _log(f"RTSTRUCT recebido no caso {cid}: {len(ptvs)} PTV(s)" + (" — aprovado" if aprovado else ""))
    pc = config()["plano"]
    if aprovado:
        _gravar_caso(cid, estruturas_aprovadas={"por": str(getattr(ds, "ReviewerName", "") or "sistema externo"),
                                                "quando": dt.datetime.now().isoformat(timespec="seconds")},
                     estado="estruturas aprovadas")
    if pc["ativo"] and ptvs and (pc["gatilho"] == "ptv" or aprovado):
        _fila.put(("planejar", cid))
    return cid


# ------------------------------------------------------------------------------------------------ etapas
def _sessao(cid: str):
    from tps.app.sessao import Sessao
    s = Sessao()
    s.carregar_ct(_pasta_casos() / cid, sem_rtstruct=True)
    return s


def contornar(cid: str, progresso=None) -> dict:
    from tps.exportacao.rtstruct import exportar_rtstruct
    cfg = config()["contorno"]
    _gravar_caso(cid, estado="contornando", evento="contorno automático iniciado")
    s = _sessao(cid)
    sit = identificar_sitio(s)
    criadas, avisos = ["BODY"] if "BODY" in s.estruturas else [], []
    ia = False
    if cfg.get("ia", True):
        try:
            from tps.ia.segmentacao import executavel
            ia = executavel() is not None
        except Exception:  # noqa: BLE001
            ia = False
    if ia:
        try:
            r = s.segmentar_ia(_orgaos(sit["sitio"], sit["sexo"]), suavizar_mm=float(cfg.get("suavizar_mm", 2.0)))
            criadas += list(r.get("criadas") or [])
        except Exception as e:  # noqa: BLE001
            avisos.append(f"IA falhou: {e}")
    else:
        avisos.append("Contorno por IA não instalado: só BODY" + (" e pulmões" if sit["sitio"] in ("torax", "mama") else ""))
        if sit["sitio"] in ("torax", "mama"):
            try:
                criadas += s.orgaos_automaticos(["pulmoes"])["criadas"]
            except Exception as e:  # noqa: BLE001
                avisos.append(str(e))
    arq = _pasta_casos() / cid / f"RS_auto_{dt.datetime.now():%Y%m%d_%H%M%S}.dcm"
    exportar_rtstruct(s, arq, rotulo="TPS_AUTO")
    d = _gravar_caso(cid, estado="contornado — aguardando alvos", sitio=sit["sitio"], sitio_como=sit["como"],
                     sexo=sit["sexo"], estruturas_auto=criadas, avisos_contorno=avisos,
                     evento=f"contorno automático: {', '.join(criadas) or 'nada'} (sítio {sit['sitio']}, {sit['como']})")
    _log(f"Caso {cid}: contornado ({len(criadas)} estruturas, sítio {sit['sitio']})")
    return d


def grupos_de_ptv(nomes: list[str]) -> list[dict]:
    """PTVs com prescrição no nome agrupados por número de frações (boost integrado = mesmo plano)."""
    ptvs = [p for p in (prescricao_do_nome(n) for n in nomes) if p]
    grupos: dict[int, list] = {}
    for p in ptvs:
        grupos.setdefault(p["fracoes"], []).append(p)
    out = []
    for fx, ps in sorted(grupos.items()):
        ps = sorted(ps, key=lambda p: -p["dose_gy"])
        out.append({"rotulo": ps[0]["nome"][:16], "principal": ps[0], "alvos": ps, "fracoes": fx})
    return out


def _variantes(pc: dict, sit: str) -> list[str]:
    v = [t for t in (pc.get("variantes") or []) if t in ("3D", "IMRT", "VMAT")]
    return v or [pc["tecnica_por_sitio"].get(sit) or pc["tecnica_padrao"]]


def _rotulo(base: str, tec: str, n_var: int) -> str:
    if n_var <= 1:
        return base[:16]
    return f"{base[:13]}_{'3D' if tec == '3D' else tec[:2]}"


def planejar(cid: str, forcar: bool = False, progresso=None) -> dict:
    """Para cada grupo de PTVs: um plano por técnica (3D e VMAT, por padrão), cada um com até 3 tentativas — a 1ª
    simples, as seguintes corrigindo cobertura, ponto quente e restrições que falharam; fica a melhor. O físico
    escolhe entre as técnicas. Devolve o caso atualizado."""
    from tps.app.servidor import banco
    pc = config()["plano"]
    s = _sessao(cid)
    s.definir_maquina(pc["maquina"], pc.get("energia", "6MV"))
    grupos = grupos_de_ptv(list(s.estruturas))
    if not grupos:
        raise ValueError("Nenhum PTV com a prescrição no nome (ex.: PTVPelve45@25)")
    ja = {p["rotulo"] for p in (_ler_caso(cid).get("planos") or [])}
    feitos = list(_ler_caso(cid).get("planos") or [])
    _gravar_caso(cid, estado="planejando", evento=f"plano automático: {', '.join(g['rotulo'] for g in grupos)}")
    info_caso = _ler_caso(cid)
    for gr in grupos:
        p0 = gr["principal"]
        sit = next((x for x in (sitio_do_texto(a["sitio_txt"]) for a in gr["alvos"]) if x), None) \
            or info_caso.get("sitio") or "pelve"
        if sit == "pelve":
            sit = "pelve_ginecologica" if info_caso.get("sexo") == "F" else ("prostata" if p0["dose_gy"] >= 60 else "reto")
        if sit in ("torax", "abdome"):
            sit = "pulmao" if sit == "torax" else "reto"
        variantes = _variantes(pc, sit)
        for tec in variantes:
            rot = _rotulo(gr["rotulo"], tec, len(variantes))
            if rot in ja and not forcar:
                continue
            registro = {"rotulo": rot, "grupo": gr["rotulo"],
                        "alvos": [f"{a['nome']} ({a['dose_gy']:g} Gy/{a['fracoes']} fx)" for a in gr["alvos"]],
                        "sitio": sit, "tecnica": tec, "quando": dt.datetime.now().isoformat(timespec="seconds")}
            try:
                if progresso:
                    progresso(f"{rot}: planejando ({tec})")
                avisos, hist = _com_tentativas(s, gr, sit, tec, pc, rot, progresso)
                f = s.salvar()
                p = s.plano
                banco().registrar_plano(f.stem, s.paciente.get("id", ""), s.paciente.get("nome", ""), p.rotulo, p.estado,
                                        s.maquina.id, str(f), "automacao", None)
                banco().auditar("automacao", "plano_automatico", f"{s.paciente.get('id', '')}/{cid}",
                                {"plano": p.rotulo, "tecnica": tec, "sitio": sit, "tentativas": len(hist)})
                r = s.resultado or {}
                ind = r.get("indices") or {}
                registro.update({"arquivo": str(f), "estado": "pronto — aguardando o físico", "avisos": avisos[:20],
                                 "hi": round(float(r["hi"]), 3) if r.get("hi") is not None else None,
                                 "v95": ind.get("v95_pct"), "dmax_alvo_pct": ind.get("dmax_alvo_pct"),
                                 "tecnica": p.tecnica, "tentativas": hist,
                                 "escolhida": next((h["tentativa"] for h in hist if h.get("escolhida")), None)})
                if pc.get("parecer_claude"):
                    registro["parecer"] = _parecer(s)
            except Exception as e:  # noqa: BLE001
                registro.update({"estado": f"erro: {e}"[:300]})
                _log(f"Caso {cid}: plano {rot} falhou: {e}")
                _estado["ultimo_erro"] = f"{cid}/{rot}: {e}"
            feitos = [x for x in feitos if x.get("rotulo") != rot] + [registro]
            _gravar_caso(cid, planos=feitos, evento=f"plano {rot}: {registro.get('estado')}")
    ok = all(str(x.get("estado", "")).startswith("pronto") for x in feitos)
    d = _gravar_caso(cid, estado="planos prontos — o físico escolhe" if ok else "plano com erro — ver detalhes")
    _log(f"Caso {cid}: {d['estado']}")
    return d


def _nota(s, gr: dict) -> dict:
    """Penalidade do plano: cobertura (V95 de cada alvo com a sua dose), ponto quente e restrições que falharam."""
    import numpy as np
    r = s.resultado or {}
    ind = r.get("indices") or {}
    rest = r.get("restricoes") or {}
    falhas = [x for x in rest.get("resultados") or [] if not x.get("ok")]
    lim = [x for x in falhas if x.get("tipo") == "limite"]
    total = s.dose_fx * s.plano.prescricao.fracoes if s.dose_fx is not None else None
    cob = {}
    for a in gr["alvos"]:
        e = s.estruturas.get(a["nome"])
        if total is not None and e is not None and e.mascara.any():
            cob[a["nome"]] = round(float((total[e.mascara] >= 0.95 * a["dose_gy"]).mean() * 100), 1)
    rx = s.plano.prescricao.dose_total_gy
    dmax_pct = round(float(r["dmax_gy"]) / rx * 100, 1) if r.get("dmax_gy") and rx else None
    pen = sum(max(0.0, 95.0 - v) * 4 for v in cob.values())
    if dmax_pct:
        pen += max(0.0, dmax_pct - 107.0) * 3
    pen += 10 * len(lim) + 2 * (len(falhas) - len(lim))
    return {"penalidade": round(float(pen), 2), "v95": cob, "dmax_pct": dmax_pct, "hi": ind.get("hi") or r.get("hi"),
            "falhas": [f"{x.get('estrutura')} {x.get('metrica')} {x.get('valor')}/{x.get('limite')}" for x in falhas][:12],
            "_falhas": falhas, "cobertura_baixa": any(v < 95 for v in cob.values()),
            "quente": bool(dmax_pct and dmax_pct > 107), "rx_ok": bool(np.isfinite(pen))}


def _com_tentativas(s, gr: dict, sit: str, tec: str, pc: dict, rot: str, progresso=None):
    n = max(1, min(3, int(pc.get("tentativas", 3) or 1)))
    melhor, hist, ant = None, [], None
    for k in range(n):
        rot_k = rot if k == 0 else f"{rot[:14]}~{k}"
        if progresso and k:
            progresso(f"{rot}: tentativa {k + 1} ({', '.join(_o_que_corrigir(ant)) or 'refinando'})")
        avisos = _fazer_plano(s, gr, sit, tec, pc, rot_k, k, ant)
        nota = _nota(s, gr)
        resumo = {"tentativa": k + 1, "penalidade": nota["penalidade"], "v95": nota["v95"], "dmax_pct": nota["dmax_pct"],
                  "hi": nota["hi"], "falhas": nota["falhas"],
                  "corrigiu": _o_que_corrigir(ant) if ant else []}
        hist.append(resumo)
        if melhor is None or nota["penalidade"] < melhor[0]["penalidade"] - 1e-6:
            if melhor is not None:
                s.remover_plano(melhor[1])
            melhor = (nota, rot_k, avisos, resumo)
        else:
            s.remover_plano(rot_k)
        if nota["penalidade"] <= 0:
            break
        ant = nota
    nota, rot_m, avisos, resumo = melhor
    resumo["escolhida"] = True
    s.trocar_plano(rot_m)
    if rot_m != rot:
        s.renomear_plano(rot)
    s._registrar(f"Plano automático {rot}: tentativa {resumo['tentativa']} de {len(hist)} escolhida "
                 f"(penalidade {nota['penalidade']})")
    return avisos + [f"Tentativa {resumo['tentativa']} de {len(hist)} escolhida (penalidade {nota['penalidade']})"], hist


def _o_que_corrigir(nota: dict | None) -> list[str]:
    if not nota:
        return []
    o = []
    if nota.get("cobertura_baixa"):
        o.append("cobertura")
    if nota.get("quente"):
        o.append("ponto quente")
    if nota.get("_falhas"):
        o.append("órgãos de risco")
    return o


def _uniao_alvos(s, gr: dict) -> str:
    """Um alvo só para conformar campos/arcos quando o grupo tem vários PTVs (boost integrado, mama + FSC)."""
    nomes = [a["nome"] for a in gr["alvos"] if a["nome"] in s.estruturas]
    if len(nomes) <= 1:
        return nomes[0]
    return _uniao(s, nomes, "zPTV_TOTAL")


def _uniao(s, nomes: list, final: str) -> str:
    if final in s.estruturas:
        return final
    atual = nomes[0]
    for i, n in enumerate(nomes[1:]):
        novo = final if i == len(nomes) - 2 else f"{final[:10]}_U{i}"
        if novo not in s.estruturas:
            s.criar_estrutura(novo, "uniao", atual, n)
        atual = novo
    return final


def _fsc_e_mama(gr: dict) -> tuple:
    fsc = [a for a in gr["alvos"] if RE_FSC.search(a["nome"])]
    mama = [a for a in gr["alvos"] if a not in fsc]
    return (mama[0] if mama else None), fsc


def _fazer_plano(s, gr: dict, sit: str, tec: str, pc: dict, rotulo: str | None = None, tentativa: int = 0,
                 anterior: dict | None = None) -> list[str]:
    import numpy as np
    p0 = gr["principal"]
    rotulo = rotulo or gr["rotulo"]
    alvo = p0["nome"]
    avisos = []
    corr = _o_que_corrigir(anterior)
    if tec == "3D":
        modelo = MODELO_3D.get(sit)
        ajustes = {}
        if tentativa:
            if "cobertura" in corr:
                ajustes["margem_extra_mm"] = 2.0 * tentativa
            if "ponto quente" in corr or tentativa >= 2:
                ajustes["fif_fino"] = True
        fsc = []
        if sit == "mama":
            from tps.planejamento import modelos_plano as MP
            m_alvo, fsc = _fsc_e_mama(gr)
            if m_alvo:
                alvo = m_alvo["nome"]
            lado = MP.lateralidade(s.estruturas[alvo].mascara, s._corpo(), s.grade.x)
            modelo = "mama_e" if lado == "E" else "mama_d"
            if fsc:                                 # mono-isocêntrico: iso na junção, tangentes com Y2 fechado
                g = s.grade
                mm = s.estruturas[alvo].mascara
                mf = np.zeros_like(mm)
                for a in fsc:
                    mf |= s.estruturas[a["nome"]].mascara
                zm = g.z[np.nonzero(mm.any(axis=(1, 2)))[0]]
                zf = g.z[np.nonzero(mf.any(axis=(1, 2)))[0]]
                zj = 0.5 * (zm.max() + zf.min()) if zf.min() > zm.max() else float(zf.min())
                kk, jj, ii = np.nonzero(mm)
                k2, j2, i2 = np.nonzero(mf)
                xi = 0.5 * (float(g.x[ii].mean()) + float(g.x[i2].mean()))
                yi = 0.5 * (float(g.y[jj].mean()) + float(g.y[j2].mean()))
                corpo = s._corpo()
                kz = int(np.argmin(np.abs(g.z - zj)))
                ix = int(np.argmin(np.abs(g.x - xi)))
                passo_y = float(g.y[1] - g.y[0])
                for _ in range(60):                     # o isocentro dentro do paciente (desce para posterior)
                    jy = int(np.argmin(np.abs(g.y - yi)))
                    if corpo[kz, jy, ix]:
                        break
                    yi += abs(passo_y)
                ajustes["isocentro"] = [round(xi, 1), round(yi + 10.0, 1), round(float(zj), 1)]
                ajustes["fechar_y"] = "y2"
        if modelo:
            out = s.aplicar_modelo(modelo, rotulo, p0["dose_gy"], p0["fracoes"], alvo, calcular=not fsc, ajustes=ajustes)
            if fsc:
                avisos += _campos_fsc(s, fsc, out["modelo_aplicado"]["opcoes"], tentativa)
            avisos += list(s.avisos or [])
            return avisos
        avisos.append(f"Sem modelo 3D para {sit}: feito em IMRT")
        tec = "IMRT"
    s.novo_plano(rotulo, p0["dose_gy"], p0["fracoes"], alvo, "media_alvo")
    conf = _uniao_alvos(s, gr)
    if conf != alvo:                                    # isocentro no centro do conjunto de alvos
        s.definir_isocentro(estrutura=conf)
    if tec == "VMAT":
        if sit == "mama":                               # arcos parciais pelo lado da mama (sem atravessar o tórax)
            from tps.planejamento import modelos_plano as MP
            m_alvo, _ = _fsc_e_mama(gr)
            lado = MP.lateralidade(s.estruturas[(m_alvo or p0)["nome"]].mascara, s._corpo(), s.grade.x)
            ini, fim = (295.0, 135.0) if lado == "E" else (225.0, 65.0)
            s.campos_vmat(n_arcos=2, inicio=ini, fim=fim, alvo=conf)
        else:
            s.campos_vmat(n_arcos=3 if tentativa >= 2 else 2, alvo=conf)
    else:
        s.campos_imrt(9 if tentativa >= 2 else 7, alvo=conf)
    texto = (f"{sit.replace('_', ' ')}; {p0['dose_gy']:g} Gy em {p0['fracoes']} frações; alvos: "
             + ", ".join(f"{a['nome']} {a['dose_gy']:g} Gy" for a in gr["alvos"]))
    motor = pc.get("motor", "auto")
    try:
        r = s.assistente_objetivos(texto, sit if sit in _sitios_assistente() else None, False, motor)
    except Exception as e:  # noqa: BLE001
        if motor == "claude":
            raise
        avisos.append(f"Assistente: {e}; usando as regras")
        r = s.assistente_objetivos(texto, sit if sit in _sitios_assistente() else None, False, "regras")
    if r.get("criar"):
        s.assistente_objetivos("", None, True, "regras", {"objetivos": [], "criar": r["criar"]})
    objs = [o for o in r.get("objetivos") or [] if o.get("estrutura") in s.estruturas]
    doses = {a["nome"]: a["dose_gy"] for a in gr["alvos"]}
    objs = [o for o in objs if not (o.get("tipo") == "alvo" and o.get("estrutura") in doses)]
    objs = [{"estrutura": n, "tipo": "alvo", "dose_gy": d, "peso": 5.0} for n, d in doses.items()] + objs
    if tentativa and anterior:
        objs = _objetivos_mais_agressivos(objs, anterior, doses, tentativa)
        avisos.append(f"Tentativa {tentativa + 1}: objetivos reforçados ({', '.join(corr) or 'refino'})")
    avisos += [f"Assistente ({r.get('motor', 'regras')}): {a}" for a in (r.get("avisos") or [])]
    chave = "vmat" if tec == "VMAT" else "imrt"
    s.calcular({chave: {"objetivos": objs, "perfil": pc.get("perfil", "tps")}})
    avisos += list(s.avisos or [])
    return avisos


def _objetivos_mais_agressivos(objs: list, ant: dict, doses: dict, k: int) -> list:
    """Tentativa k (1, 2): reforça o que falhou — órgãos que passaram do limite (dose ×0,9 / ×0,8, peso ×2 / ×3),
    cobertura (DVH mínimo no alvo) e ponto quente (máximo no alvo)."""
    import copy
    import re
    objs = copy.deepcopy(objs)
    f_dose, f_peso = (0.9, 2.0) if k == 1 else (0.8, 3.0)
    for x in ant.get("_falhas") or []:
        n, met, val = x.get("estrutura"), str(x.get("metrica", "")), float(x.get("limite") or 0)
        if not n or n in doses:
            continue
        mesmos = [o for o in objs if o.get("estrutura") == n and o.get("tipo") != "alvo"]
        for o in mesmos:
            if o.get("tipo") == "dvh" and o.get("volume_pct"):
                o["volume_pct"] = round(float(o["volume_pct"]) * f_dose, 1)
            elif o.get("dose_gy"):
                o["dose_gy"] = round(float(o["dose_gy"]) * f_dose, 2)
            o["peso"] = round(float(o.get("peso", 1.0)) * f_peso, 2)
        if not mesmos and val > 0:
            if met == "Dmax" or re.fullmatch(r"D[\d.]+cc", met):
                objs.append({"estrutura": n, "tipo": "max", "dose_gy": round(val * f_dose, 2), "peso": f_peso})
            elif met == "Dmean":
                objs.append({"estrutura": n, "tipo": "media", "dose_gy": round(val * f_dose, 2), "peso": f_peso})
            else:
                mm = re.fullmatch(r"V([\d.]+)Gy", met)
                if mm:
                    objs.append({"estrutura": n, "tipo": "dvh", "dose_gy": float(mm.group(1)),
                                 "volume_pct": round(val * f_dose, 1), "peso": f_peso})
    for n, d in doses.items():
        if ant.get("v95", {}).get(n, 100) < 95:
            objs.append({"estrutura": n, "tipo": "dvh_min", "dose_gy": round(0.97 * d, 2), "volume_pct": 97.0,
                         "peso": 5.0 * k})
            for o in objs:
                if o.get("estrutura") == n and o.get("tipo") == "alvo":
                    o["peso"] = round(float(o.get("peso", 5.0)) * (1 + 0.5 * k), 2)
    if ant.get("quente"):
        for n, d in doses.items():
            objs.append({"estrutura": n, "tipo": "max", "dose_gy": round(1.05 * d, 2), "peso": 2.0 * k})
    return objs


def _campos_fsc(s, fsc: list, opcoes: dict, tentativa: int = 0) -> list[str]:
    """Campos diretos na drenagem (FSC/axila): AP com Y1 fechado no isocentro (junção com as tangentes); se a
    cobertura ficar baixa, entra também o PA. Os pesos são acertados para a dose média chegar à prescrição dela."""
    import numpy as np
    avisos = []
    nomes = [a["nome"] for a in fsc]
    alvo_f = nomes[0] if len(nomes) == 1 else _uniao(s, nomes, "zFSC_TOTAL")
    dose_f = max(a["dose_gy"] for a in fsc)
    mg = 7.0 + (2.0 * tentativa if tentativa else 0.0)
    base = {"modo": "conformado", "alvo": alvo_f, "margem": {"x1": mg, "x2": mg, "y1": mg, "y2": mg},
            "ponto_encontro": "fora", "blindagens": [], "fechar_y": "y1", "colimador": 0}
    s.salvar_campo({**base, "nome": "FSC_AP", "gantry": 0.0, "peso": 1.0})
    campos = ["FSC_AP"]
    mascara = s.estruturas[alvo_f].mascara

    sem_fif = {k: v for k, v in opcoes.items() if k != "field_in_field"}

    def acertar() -> float:
        """Escala os campos da FSC para a dose média dela chegar à prescrição (as tangentes quase não passam ali;
        a normalização é na mama): fecha num passo com a dose por UM de cada campo. Devolve o V95 estimado."""
        s.calcular(sem_fif)
        tot = (s.dose_fx * s.plano.prescricao.fracoes)[mascara]
        fr = s.plano.prescricao.fracoes
        f = sum(float(s._campo(n).mu or 0.0) * s.doses_um[n][mascara] for n in campos) * fr
        f_m = float(np.mean(f)) if np.size(f) else 0.0
        if f_m <= 0:
            return float((tot >= 0.95 * dose_f).mean() * 100)
        k = float(np.clip((dose_f - (float(tot.mean()) - f_m)) / f_m, 0.05, 20.0))
        for n in campos:
            c = s._campo(n)
            spec = dict(c.__dict__.get("_spec") or {})
            spec.update(nome=n, nome_antigo=n, peso=max(0.01, float(spec.get("peso", c.peso)) * k))
            s.salvar_campo(spec)
        return float(((tot + (k - 1.0) * f) >= 0.95 * dose_f).mean() * 100)
    v95 = acertar()
    if v95 < 90.0 or tentativa >= 2:                    # drenagem profunda: AP + PA
        peso_ap = float((s._campo("FSC_AP").__dict__.get("_spec") or {}).get("peso", 1.0))
        s.salvar_campo({**base, "nome": "FSC_PA", "gantry": 180.0, "peso": max(0.01, 0.4 * peso_ap)})
        campos.append("FSC_PA")
        acertar()
    s.calcular(opcoes)
    tot = s.dose_fx * s.plano.prescricao.fracoes
    v95 = float((tot[mascara] >= 0.95 * dose_f).mean() * 100)
    avisos.append(f"FSC: {'AP + PA' if len(campos) > 1 else 'campo AP direto'} (V95 {v95:.0f}%, "
                  f"média {float(tot[mascara].mean()):.1f} Gy)")
    return avisos


def _sitios_assistente() -> set:
    from tps.planejamento import assistente as A
    return set(A.carregar())


def _parecer(s) -> dict | None:
    try:
        from tps.app import copiloto as CP
        from tps.planejamento import assistente_claude as C
        if not C.estado()["ativo"]:
            return None
        r = CP.conversar(s, "Plano feito automaticamente. Avalie o plano (dose, restrições, cobertura, homogeneidade) "
                            "e dê o seu parecer com dar_parecer. NÃO altere nada.", somente_leitura=True, novo=True)
        return r.get("parecer") or {"resumo": r.get("resposta", "")[:1500]}
    except Exception as e:  # noqa: BLE001
        return {"erro": str(e)[:200]}


def desaprovar_estruturas(s, usuario: str, papel: str, motivo: str = "") -> dict:
    """Desaprova (fica registrado quem e por quê) e grava um RTSTRUCT não aprovado como o mais recente do caso."""
    from tps.exportacao.rtstruct import exportar_rtstruct
    r = s.desaprovar_estruturas(usuario, papel, motivo)
    cid = caso_da_pasta(s.pasta_ct)
    if cid:
        agora = dt.datetime.now()
        arq = _pasta_casos() / cid / f"RS_desaprovado_{agora:%Y%m%d_%H%M%S}.dcm"
        exportar_rtstruct(s, arq, rotulo="DESAPROVADO")
        _gravar_caso(cid, estruturas_aprovadas=None, estado="estruturas desaprovadas",
                     evento=f"estruturas DESAPROVADAS por {usuario} ({papel})" + (f": {motivo}" if motivo else ""))
    return r


def aprovar_estruturas(s, usuario: str, papel: str) -> dict:
    """Aprovação das estruturas na sessão: grava o RTSTRUCT aprovado no caso (adota a TC como caso, se preciso) e,
    com o plano automático ligado, põe o plano na fila."""
    from tps.exportacao.rtstruct import exportar_rtstruct
    if s.tipo != "ct" or s.ct is None:
        raise ValueError("Aprovação de estruturas só em TC de paciente")
    nomes = [n for n, e in s.estruturas.items() if e.mascara is not None and e.mascara.any()]
    ptvs = [p for p in (prescricao_do_nome(n) for n in nomes) if p]
    cid = caso_da_pasta(s.pasta_ct)
    if cid is None:                                       # TC aberta fora dos casos: vira um caso
        arqs = [f for f in sorted(Path(s.pasta_ct).iterdir()) if f.is_file() and s._e_ct(f)]
        ds = pydicom.dcmread(str(arqs[0]), stop_before_pixels=True, force=True)
        with _trava:
            idx = _indice()
            cid = idx.get("series", {}).get(str(ds.SeriesInstanceUID))
            if cid is None:
                cid = _id_caso(ds)
                (_pasta_casos() / cid).mkdir(parents=True)
                for f in arqs:
                    shutil.copy2(f, _pasta_casos() / cid / f.name)
                idx.setdefault("series", {})[str(ds.SeriesInstanceUID)] = cid
                idx.setdefault("referenciais", {})[str(getattr(ds, "FrameOfReferenceUID", ""))] = cid
                idx.setdefault("estudos", {})[str(getattr(ds, "StudyInstanceUID", ""))] = cid
                _gravar_indice(idx)
                _gravar_caso(cid, paciente_id=str(getattr(ds, "PatientID", "")),
                             paciente=str(getattr(ds, "PatientName", "")), cortes=len(arqs), estado="recebido",
                             evento="TC adotada como caso na aprovação das estruturas")
    agora = dt.datetime.now()
    arq = _pasta_casos() / cid / f"RS_aprovado_{agora:%Y%m%d_%H%M%S}.dcm"
    rs = exportar_rtstruct(s, arq, rotulo="APROVADO")
    rs.ApprovalStatus = "APPROVED"
    rs.ReviewDate, rs.ReviewTime = agora.strftime("%Y%m%d"), agora.strftime("%H%M%S")
    rs.ReviewerName = usuario
    rs.save_as(arq, enforce_file_format=True)
    s.estruturas_aprovadas = {"por": usuario, "papel": papel, "quando": agora.strftime("%Y-%m-%d %H:%M"),
                              "nomes": list(s.estruturas)}
    _gravar_caso(cid, estruturas_aprovadas={"por": usuario, "papel": papel, "quando": agora.isoformat(timespec="seconds"),
                                            "arquivo": arq.name, "estruturas": len(nomes)},
                 ptvs=[p["nome"] for p in ptvs], estado="estruturas aprovadas",
                 evento=f"estruturas aprovadas por {usuario} ({papel}); PTVs: {', '.join(p['nome'] for p in ptvs) or 'nenhum'}")
    pc = config()["plano"]
    fila = bool(pc["ativo"] and ptvs)
    if fila:
        _fila.put(("planejar", cid))
        _garantir_executor()
    _log(f"Estruturas aprovadas no caso {cid} por {usuario}" + (" — plano na fila" if fila else ""))
    return {"caso": cid, "arquivo": str(arq), "ptvs": ptvs, "plano_na_fila": fila,
            "aviso": None if ptvs else "Nenhum PTV com a prescrição no nome (ex.: PTVPelve45@25): o plano automático "
                                        "não foi disparado"}


# ------------------------------------------------------------------------------------------------ varredura e fila
def varrer(pasta: Path, estabilizar_s: float) -> list[tuple]:
    """Lê os DICOM novos da pasta; devolve as séries de TC prontas e os RTSTRUCT novos."""
    series: dict[str, dict] = _estado.setdefault("_series", {})
    rts = []
    for f in pasta.rglob("*"):
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        chave = str(f)
        if _vistos.get(chave) == (st.st_mtime, st.st_size):
            continue
        try:
            ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
        except Exception:  # noqa: BLE001
            _vistos[chave] = (st.st_mtime, st.st_size)
            continue
        _vistos[chave] = (st.st_mtime, st.st_size)
        mod = getattr(ds, "Modality", "")
        if mod == "CT" and getattr(ds, "SeriesInstanceUID", None):
            sr = series.setdefault(str(ds.SeriesInstanceUID), {"arquivos": set(), "ultimo": 0.0, "ds": ds})
            sr["arquivos"].add(f)
            sr["ultimo"] = time.time()
        elif mod == "RTSTRUCT":
            rts.append((f, ds))
    prontas = []
    for uid, sr in list(series.items()):
        if time.time() - sr["ultimo"] >= estabilizar_s:
            prontas.append((sorted(sr["arquivos"]), sr["ds"]))
            series.pop(uid)
    return prontas, rts


def _laco_varredura(ger: int):
    while not _parar.is_set() and ger == _geracao[0]:
        cfg = config()
        try:
            pastas = [Path(cfg["pasta_entrada"])] if cfg.get("pasta_entrada") else []
            if cfg["receptor_dicom"]["ativo"]:
                pastas.append(paths.home() / "recebidos")
            for pasta in pastas:
                if not pasta.is_dir():
                    continue
                prontas, rts = varrer(pasta, float(cfg.get("estabilizar_s", 60)))
                ja = _indice().get("series", {})
                for arqs, ds in prontas:
                    if str(ds.SeriesInstanceUID) not in ja:
                        receber_serie(arqs, ds)
                for f, ds in rts:
                    receber_rtstruct(f, ds)
        except Exception as e:  # noqa: BLE001
            _log(f"varredura: {e}")
        _parar.wait(max(5.0, float(cfg.get("intervalo_s", 30))))


def _executor(ger: int):
    while ger == _geracao[0]:
        try:
            tipo, cid = _fila.get(timeout=2)
        except queue.Empty:
            continue
        with _trava:
            _estado["atual"] = f"{tipo} {cid}"
        try:
            if tipo == "contornar":
                contornar(cid)
            elif tipo == "planejar":
                planejar(cid)
        except Exception as e:  # noqa: BLE001
            _estado["ultimo_erro"] = f"{tipo} {cid}: {e}"
            _log(f"{tipo} {cid} falhou: {e}")
            try:
                _gravar_caso(cid, estado=f"erro em {tipo}: {e}"[:200], evento=traceback.format_exc()[-800:])
            except Exception:  # noqa: BLE001
                pass
        finally:
            with _trava:
                _estado["atual"] = None


def _garantir_executor():
    with _trava:
        if not any(t.name == "automacao-executor" and t.is_alive() for t in _threads):
            t = threading.Thread(target=_executor, args=(_geracao[0],), daemon=True, name="automacao-executor")
            t.start()
            _threads.append(t)


def _iniciar_receptor(cfg: dict):
    global _receptor
    from pynetdicom import AE, AllStoragePresentationContexts, evt
    destino = paths.home() / "recebidos"
    destino.mkdir(parents=True, exist_ok=True)

    def ao_receber(event):
        ds = event.dataset
        ds.file_meta = event.file_meta
        pasta = destino / re.sub(r"[^A-Za-z0-9_.\-]+", "_", str(getattr(ds, "SeriesInstanceUID", "serie")))
        pasta.mkdir(exist_ok=True)
        ds.save_as(pasta / f"{ds.SOPInstanceUID}.dcm", enforce_file_format=True)
        return 0x0000
    ae = AE(ae_title=str(cfg.get("ae", "TPS"))[:16])
    ae.supported_contexts = AllStoragePresentationContexts
    _receptor = ae.start_server(("0.0.0.0", int(cfg.get("porta", 11112))), block=False,
                                evt_handlers=[(evt.EVT_C_STORE, ao_receber)])
    _log(f"Receptor DICOM ouvindo na porta {cfg.get('porta', 11112)} (AE {cfg.get('ae', 'TPS')})")


def iniciar():
    cfg = config()
    if not cfg["ativo"]:
        return
    _parar.clear()
    with _trava:
        _geracao[0] += 1
        _estado["rodando"] = True
    if cfg["receptor_dicom"]["ativo"] and _receptor is None:
        try:
            _iniciar_receptor(cfg["receptor_dicom"])
        except Exception as e:  # noqa: BLE001
            _log(f"Receptor DICOM não iniciou: {e}")
    t = threading.Thread(target=_laco_varredura, args=(_geracao[0],), daemon=True, name="automacao-varredura")
    t.start()
    _threads.append(t)
    _garantir_executor()
    _log("Automação ligada" + (f": pasta {cfg['pasta_entrada']}" if cfg["pasta_entrada"] else ""))


def parar():
    global _receptor
    _parar.set()
    if _receptor is not None:
        try:
            _receptor.shutdown()
        except Exception:  # noqa: BLE001
            pass
        _receptor = None
    with _trava:
        _geracao[0] += 1
        _estado["rodando"] = False
        _threads.clear()


def enfileirar(tipo: str, cid: str):
    if tipo not in ("contornar", "planejar"):
        raise ValueError("Etapa: contornar ou planejar")
    caso(cid)
    _fila.put((tipo, cid))
    _garantir_executor()
