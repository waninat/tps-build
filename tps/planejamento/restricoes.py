"""Restrições de dose por órgão (QUANTEC/ICRU, editáveis pela clínica) e nomes padronizados AAPM TG-263.

Os limites vêm de dados/restricoes/padrao_quantec.yaml; se existir <home>/restricoes/clinica.yaml, ele substitui o
padrão. Os nomes das estruturas do paciente são mapeados para o TG-263 (aceita nomes em português e variações
comuns: "MEDULA", "Pulmão D", "cabeca femoral esq"...). Pulmões e rins somam esquerdo + direito quando não há a
estrutura bilateral.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

from tps import paths

PADRAO = Path(__file__).resolve().parents[2] / "dados" / "restricoes" / "padrao_quantec.yaml"

# (TG-263, aliases já normalizados: maiúsculas, sem acento, sem espaço/_/-)
_BASE = {
    "SpinalCord": ["MEDULA", "MEDULAESPINHAL", "MEDULAESPINAL", "CORD", "SPINALCORD"],
    "Brainstem": ["TRONCO", "TRONCOCEREBRAL", "TRONCOENCEFALICO", "BRAINSTEM"],
    "OpticChiasm": ["QUIASMA", "QUIASMAOPTICO", "CHIASM", "OPTICCHIASM"],
    "Cochlea": ["COCLEA", "COCHLEA"],
    "Parotid": ["PAROTIDA", "PAROTID"],
    "OpticNrv": ["NERVOOPTICO", "NOPTICO", "OPTICNERVE", "OPTICNRV"],
    "Larynx": ["LARINGE", "LARYNX"],
    "Esophagus": ["ESOFAGO", "ESOPHAGUS"],
    "Heart": ["CORACAO", "HEART"],
    "Lungs": ["PULMOES", "PULMAOTOTAL", "PULMOESTOTAL", "LUNGS", "LUNGTOTAL"],
    "Lung": ["PULMAO", "LUNG"],
    "Liver": ["FIGADO", "LIVER"],
    "Kidneys": ["RINS", "KIDNEYS"],
    "Kidney": ["RIM", "KIDNEY"],
    "Bowel_Small": ["INTESTINODELGADO", "DELGADO", "SMALLBOWEL", "BOWELSMALL"],
    "Bag_Bowel": ["ALCAS", "ALCASINTESTINAIS", "BOWELBAG", "BAGBOWEL", "CAVIDADEPERITONEAL"],
    "Rectum": ["RETO", "RECTUM"],
    "Bladder": ["BEXIGA", "BLADDER"],
    "PenileBulb": ["BULBOPENIANO", "BULBO", "PENILEBULB"],
    "Femur_Head": ["CABECAFEMORAL", "CABECADOFEMUR", "FEMUR", "FEMURHEAD", "FEMORALHEAD"],
    "Lens": ["CRISTALINO", "LENS"],
    "Brain": ["ENCEFALO", "CEREBRO", "BRAIN"],
    "External": ["BODY", "CORPO", "EXTERNAL", "EXTERNO", "PELE", "SKIN", "CONTORNO"],
}
_LATERAIS = {"Cochlea", "Parotid", "OpticNrv", "Lung", "Kidney", "Femur_Head", "Lens"}
_DIR = ("DIREITO", "DIREITA", "DIR", "D", "R", "RIGHT", "RT")
_ESQ = ("ESQUERDO", "ESQUERDA", "ESQ", "E", "L", "LEFT", "LT")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def nome_tg263(nome: str) -> str | None:
    """Nome TG-263 da estrutura, ou None se não reconhecida (alvos ficam None: são tratados à parte)."""
    if re.fullmatch(r"[A-Z0-9_ ]+", nome or "") and nome in _todos_tg263():
        return nome
    n = _norm(nome)
    for tg, aliases in _BASE.items():
        for a in sorted(aliases, key=len, reverse=True):
            if n == a:
                return tg if tg not in _LATERAIS else None
            if tg in _LATERAIS and n.startswith(a):
                resto = n[len(a):]
                resto = re.sub(r"^(DO|DA|DE)", "", resto)
                if resto in _DIR:
                    return f"{tg}_R"
                if resto in _ESQ:
                    return f"{tg}_L"
    return None


def _todos_tg263():
    s = set()
    for tg in _BASE:
        s |= {f"{tg}_L", f"{tg}_R"} if tg in _LATERAIS else {tg}
    return s


def carregar(arquivo: Path | None = None) -> dict:
    if arquivo is None:
        clin = paths.home() / "restricoes" / "clinica.yaml"
        arquivo = clin if clin.exists() else PADRAO
    dados = yaml.safe_load(Path(arquivo).read_text(encoding="utf-8"))
    dados["_arquivo"] = str(arquivo)
    return dados


def metrica(doses: np.ndarray, voxel_cc: float, m: str, prescricao_gy: float) -> float:
    """Valor da métrica (Gy, % ou cm³). D<n>% e V<n>% no alvo são relativos à prescrição."""
    d = np.sort(np.asarray(doses, float))[::-1]
    if d.size == 0:
        return float("nan")
    vol = d.size * voxel_cc
    if m == "Dmean":
        return float(d.mean())
    if m == "Dmax":
        return float(d[0])
    r = re.fullmatch(r"D([\d.]+)cc", m)
    if r:
        n = max(1, int(np.ceil(float(r.group(1)) / voxel_cc)))
        return float(d[min(n, d.size) - 1])
    r = re.fullmatch(r"D([\d.]+)%", m)
    if r:
        return float(np.percentile(d, 100 - float(r.group(1))))
    r = re.fullmatch(r"V([\d.]+)Gy(_cc)?", m)
    if r:
        v = float((d >= float(r.group(1))).sum())
        return v * voxel_cc if r.group(2) else v / d.size * 100
    r = re.fullmatch(r"V([\d.]+)%", m)
    if r:
        return float((d >= float(r.group(1)) / 100 * prescricao_gy).sum()) / d.size * 100
    raise ValueError(f"métrica desconhecida: {m}")


@dataclass
class Resultado:
    estrutura: str
    tg263: str
    metrica: str
    op: str
    limite: float
    valor: float
    unidade: str
    tipo: str
    fonte: str
    ok: bool
    obs: str = ""

    def json(self):
        return {k: (round(v, 2) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class Avaliacao:
    resultados: list = field(default_factory=list)
    avisos: list = field(default_factory=list)
    protocolo: str = ""

    def json(self):
        return {"protocolo": self.protocolo, "avisos": self.avisos,
                "resultados": [r.json() for r in self.resultados],
                "falhas_limite": sum(1 for r in self.resultados if not r.ok and r.tipo == "limite"),
                "falhas_objetivo": sum(1 for r in self.resultados if not r.ok and r.tipo != "limite")}


def avaliar(doses: dict, voxel_cc: float, prescricao_gy: float, fracoes: int, alvo: str | None,
            protocolo: dict | None = None) -> Avaliacao:
    """doses: {nome da estrutura: doses dos voxels (Gy, total do plano)}."""
    prot = protocolo or carregar()
    av = Avaliacao(protocolo=prot.get("nome", ""))
    dpf = prescricao_gy / max(fracoes, 1)
    if not 1.7 <= dpf <= 2.2:
        av.avisos.append(f"Dose por fração {dpf:.2f} Gy: os limites QUANTEC valem para 1,8–2,0 Gy/fração. "
                         "Converta para EQD2 ou use um protocolo próprio para hipofracionamento.")
    if alvo and alvo in doses:
        for c in prot.get("alvo", []):
            v = metrica(doses[alvo], voxel_cc, c["metrica"], prescricao_gy)
            if c["metrica"].startswith("D") and c["metrica"].endswith("%"):
                v = v / prescricao_gy * 100
            av.resultados.append(_res(alvo, "alvo", c, v))
    por_tg = {}
    for nome in doses:
        tg = nome_tg263(nome)
        if tg and tg != "External":
            por_tg.setdefault(tg, []).append(nome)
    for bil, (e, d) in (("Lungs", ("Lung_L", "Lung_R")), ("Kidneys", ("Kidney_L", "Kidney_R"))):
        if bil not in por_tg and e in por_tg and d in por_tg:
            por_tg[bil] = por_tg[e] + por_tg[d]
    for tg, lista in sorted(por_tg.items()):
        for c in prot.get("orgaos", {}).get(tg, []):
            dd = np.concatenate([np.asarray(doses[n]) for n in lista])
            v = metrica(dd, voxel_cc, c["metrica"], prescricao_gy)
            r = _res(" + ".join(lista), tg, c, v)
            if tg in ("Lungs", "Kidneys") and len(lista) > 1:
                r.obs = "soma de esquerdo + direito"
            if tg == "Lungs":
                r.obs = (r.obs + "; " if r.obs else "") + "QUANTEC: pulmões menos GTV"
            av.resultados.append(r)
    return av


def _res(nome, tg, c, v):
    lim = float(c["valor"])
    ok = (v <= lim + 1e-9) if c["op"] == "<=" else (v >= lim - 1e-9)
    m = c["metrica"]
    un = c.get("unidade") or ("cm³" if m.endswith("_cc") else "%" if m.startswith("V") else "Gy")
    return Resultado(nome, tg, m.replace("_cc", " (cm³)"), c["op"], lim, float(v), un, c.get("tipo", "limite"),
                     str(c.get("fonte", "")), bool(ok))
