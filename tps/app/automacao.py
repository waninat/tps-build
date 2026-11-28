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
              "motor": "auto", "perfil": "tps", "parecer_claude": True},
}

# palavra no nome do PTV / na descrição do exame → sítio (protocolo do assistente)
SITIOS = [
    (r"prost", "prostata"), (r"mama|breast|parede", "mama"), (r"pulm|lung", "pulmao"), (r"esof", "esofago"),
    (r"enc[eé]f|cerebr|cranio|crânio|brain|glio", "encefalo"), (r"cab|pesc|orof|laring|nasof|boca|h&n|hn", "cabeca_pescoco"),
    (r"colo|utero|útero|endom|gine|cervix|colodo", "pelve_ginecologica"), (r"reto|anal|anus|rectum", "reto"),
    (r"pelv", "pelve"), (r"t[oó]rax|thorax|chest", "torax"), (r"abd", "abdome"),
]
MODELO_3D = {"prostata": "prostata", "pulmao": "pulmao", "esofago": "esofago", "encefalo": "encefalo_total",
             "pelve_ginecologica": "colo_uterino", "reto": "reto"}          # mama: mama_d / mama_e pelo lado
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


def planejar(cid: str, forcar: bool = False, progresso=None) -> dict:
    """Um plano por grupo de PTVs ainda não planejado. Devolve o caso atualizado."""
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
        if gr["rotulo"] in ja and not forcar:
            continue
        p0 = gr["principal"]
        sit = sitio_do_texto(p0["sitio_txt"]) or info_caso.get("sitio") or "pelve"
        if sit == "pelve":
            sit = "pelve_ginecologica" if info_caso.get("sexo") == "F" else ("prostata" if p0["dose_gy"] >= 60 else "reto")
        if sit in ("torax", "abdome"):
            sit = "pulmao" if sit == "torax" else "reto"
        tec = pc["tecnica_por_sitio"].get(sit) or pc["tecnica_padrao"]
        registro = {"rotulo": gr["rotulo"], "alvos": [f"{a['nome']} ({a['dose_gy']:g} Gy/{a['fracoes']} fx)" for a in gr["alvos"]],
                    "sitio": sit, "tecnica": tec, "quando": dt.datetime.now().isoformat(timespec="seconds")}
        try:
            avisos = _fazer_plano(s, gr, sit, tec, pc, progresso)
            f = s.salvar()
            p = s.plano
            banco().registrar_plano(f.stem, s.paciente.get("id", ""), s.paciente.get("nome", ""), p.rotulo, p.estado,
                                    s.maquina.id, str(f), "automacao", None)
            banco().auditar("automacao", "plano_automatico", f"{s.paciente.get('id', '')}/{cid}",
                            {"plano": p.rotulo, "tecnica": tec, "sitio": sit})
            r = s.resultado or {}
            registro.update({"arquivo": str(f), "estado": "pronto — aguardando o físico", "avisos": avisos[:20],
                             "hi": round(float(r["hi"]), 3) if r.get("hi") is not None else None,
                             "tecnica": p.tecnica})
            if pc.get("parecer_claude"):
                registro["parecer"] = _parecer(s)
        except Exception as e:  # noqa: BLE001
            registro.update({"estado": f"erro: {e}"[:300]})
            _log(f"Caso {cid}: plano {gr['rotulo']} falhou: {e}")
            _estado["ultimo_erro"] = f"{cid}/{gr['rotulo']}: {e}"
        feitos = [x for x in feitos if x.get("rotulo") != gr["rotulo"]] + [registro]
        _gravar_caso(cid, planos=feitos, evento=f"plano {gr['rotulo']}: {registro.get('estado')}")
    ok = all(str(x.get("estado", "")).startswith("pronto") for x in feitos)
    d = _gravar_caso(cid, estado="plano pronto — aguardando o físico" if ok else "plano com erro — ver detalhes")
    _log(f"Caso {cid}: {d['estado']}")
    return d


def _fazer_plano(s, gr: dict, sit: str, tec: str, pc: dict, progresso=None) -> list[str]:
    p0 = gr["principal"]
    alvo = p0["nome"]
    avisos = []
    if tec == "3D":
        modelo = MODELO_3D.get(sit)
        if sit == "mama":
            from tps.planejamento import modelos_plano as MP
            lado = MP.lateralidade(s.estruturas[alvo].mascara, s._corpo(), s.grade.x)
            modelo = "mama_e" if lado == "E" else "mama_d"
        if modelo:
            s.aplicar_modelo(modelo, gr["rotulo"], p0["dose_gy"], p0["fracoes"], alvo, calcular=True)
            avisos += list(s.avisos or [])
            return avisos
        avisos.append(f"Sem modelo 3D para {sit}: feito em {pc['tecnica_padrao'] if pc['tecnica_padrao'] != '3D' else 'IMRT'}")
        tec = pc["tecnica_padrao"] if pc["tecnica_padrao"] != "3D" else "IMRT"
    s.novo_plano(gr["rotulo"], p0["dose_gy"], p0["fracoes"], alvo, "media_alvo")
    if tec == "VMAT":
        s.campos_vmat(n_arcos=2, alvo=alvo)
    else:
        s.campos_imrt(7, alvo=alvo)
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
    avisos += [f"Assistente ({r.get('motor', 'regras')}): {a}" for a in (r.get("avisos") or [])]
    chave = "vmat" if tec == "VMAT" else "imrt"
    s.calcular({chave: {"objetivos": objs, "perfil": pc.get("perfil", "tps")}})
    avisos += list(s.avisos or [])
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
