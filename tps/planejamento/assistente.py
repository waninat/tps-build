"""Assistente de objetivos dos planos modulados (IMRT/VMAT): protocolo por sítio + leitura do caso.

Não é um modelo de linguagem: é um conjunto de regras, transparente e auditável. Lê a descrição do caso (sítio,
doses, frações, lado, pós-operatório), olha a TC e as estruturas (alvos e doses pelos nomes, lado do alvo, órgãos
perto do alvo, órgãos que faltam, sobreposição com o PTV, distância à pele) e monta os objetivos a partir do arquivo
do sítio (dados/objetivos_sitio/*.yaml, ou o da clínica em <dados>/objetivos_sitio/). Tudo o que decidiu vai
explicado em `explicacao`, para o planejador conferir. SRS-PLN-095.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import numpy as np
import yaml

from tps import paths
from tps.planejamento.restricoes import nome_tg263

PADRAO = Path(__file__).resolve().parents[2] / "dados" / "objetivos_sitio"
FOLGA_MAX, FOLGA_MEDIA, FOLGA_VOL = 0.95, 0.90, 0.90      # mesma folga da sugestão pelas restrições
PADRAO_ALVO = re.compile(r"^(PTV|CTV|GTV|ITV)", re.IGNORECASE)
# TG-263 → id do órgão no contorno por IA (tps.ia.segmentacao.ORGAOS)
TG_PARA_IA = {"Bladder": "bexiga", "Rectum": "reto", "Bag_Bowel": "bowel_bag", "Bowel_Small": "intestino_delgado",
              "Colon_Sigmoid": "colon", "Femur_Head_L": "femur_e", "Femur_Head_R": "femur_d", "Kidney_L": "rim_e",
              "Kidney_R": "rim_d", "Liver": "figado", "Stomach": "estomago", "SpinalCord": "medula", "Heart": "coracao",
              "Esophagus": "esofago", "Lung_L": "pulmao_e", "Lung_R": "pulmao_d", "Trachea": "traqueia",
              "Thyroid": "tireoide", "Brain": "encefalo", "Breast_L": "mama_e", "Breast_R": "mama_d",
              "Parotid_L": "parotida_e", "Parotid_R": "parotida_d", "Glnd_Submand_L": "submandibular_e",
              "Glnd_Submand_R": "submandibular_d", "Eye_L": "olho_e", "Eye_R": "olho_d", "Lens_L": "cristalino_e",
              "Lens_R": "cristalino_d", "OpticNrv_L": "nervo_optico_e", "OpticNrv_R": "nervo_optico_d",
              "Cavity_Oral": "cavidade_oral", "Musc_Constrict": "constritores", "Bone_Mandible": "mandibula",
              "Anus": "canal_anal"}
BILATERAIS = {"Lungs": ("Lung_L", "Lung_R", "Pulmoes"), "Kidneys": ("Kidney_L", "Kidney_R", "Rins")}
# sítio provável pelo órgão mais perto do alvo (quando a descrição não diz)
ORGAO_SITIO = {"Bladder": "pelve_ginecologica", "Rectum": "reto", "Bag_Bowel": "pelve_ginecologica",
               "Femur_Head_L": "pelve_ginecologica", "Femur_Head_R": "pelve_ginecologica", "PenileBulb": "prostata",
               "Lung_L": "pulmao", "Lung_R": "pulmao", "Lungs": "pulmao", "Heart": "mama", "Breast_L": "mama",
               "Breast_R": "mama", "Esophagus": "esofago", "Brainstem": "encefalo", "Brain": "encefalo",
               "OpticChiasm": "encefalo", "Eye_L": "encefalo", "Eye_R": "encefalo", "Parotid_L": "cabeca_pescoco",
               "Parotid_R": "cabeca_pescoco", "Larynx": "cabeca_pescoco", "Bone_Mandible": "cabeca_pescoco",
               "Cavity_Oral": "cabeca_pescoco"}


def _sem_acento(s: str) -> str:
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def carregar() -> dict[str, dict]:
    out = {}
    for pasta in (PADRAO, paths.home() / "objetivos_sitio"):
        if not pasta.is_dir():
            continue
        for f in sorted(pasta.glob("*.yaml")):
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if d.get("id"):
                d["_arquivo"] = str(f)
                d["_clinica"] = pasta != PADRAO
                out[d["id"]] = d
    return out


def lista() -> list[dict]:
    return [{"id": k, "nome": v.get("nome", k), "sitio": v.get("sitio", ""), "fonte": v.get("fonte", ""),
             "clinica": v["_clinica"]} for k, v in carregar().items()]


# ---------------------------------------------------------------------------------------------------- texto
def _num(s: str) -> float:
    return float(s.replace(",", "."))


def interpretar(texto: str, sitios: dict | None = None) -> dict:
    """Sítio, doses (Gy), frações, lado, pós-operatório e boost integrado a partir da descrição livre."""
    sitios = sitios if sitios is not None else carregar()
    t = _sem_acento(texto)
    out: dict = {"sitio": None, "doses_gy": [], "fracoes": None, "lado": None, "pos_op": False, "sib": False,
                 "termos": []}
    melhor = (0, None)
    for k, s in sitios.items():
        achou = [p for p in s.get("palavras", []) if re.search(r"\b" + re.escape(_sem_acento(p)), t)]
        if len(achou) > melhor[0]:
            melhor = (len(achou), k)
            out["termos"] = achou
    out["sitio"] = melhor[1]
    num = r"\d+(?:[.,]\d+)?"
    for m in re.finditer(rf"((?:{num}(?:,\s+|\s+e\s+|\s*;\s*))*{num})\s*(c?gy)\b", t):
        for x in re.findall(num, m.group(1)):
            v = _num(x) / (100.0 if m.group(2) == "cgy" else 1.0)
            if 1.0 <= v <= 90 and v not in out["doses_gy"]:
                out["doses_gy"].append(v)
    m = re.search(r"(\d+)\s*(?:x|fx|fr\b|fra[cç]o(?:es)?|fracoes|fracao|sessoes|sessao|frac)", t)
    if m and 1 <= int(m.group(1)) <= 45:
        out["fracoes"] = int(m.group(1))
    # dose por fração escrita como "2 Gy/fx" ou "1,8 Gy por fração" não é dose total
    porfx = [_num(m.group(1)) for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*gy\s*(?:/|por|por\s+)\s*(?:fx|fra)", t)]
    out["doses_gy"] = [d for d in out["doses_gy"] if d not in porfx]
    out["dose_fracao_gy"] = porfx[0] if porfx else None
    if re.search(r"\b(esquerd[oa]|esq\b|left)", t):
        out["lado"] = "L"
    if re.search(r"\b(direit[oa]|dir\b|right)", t):
        out["lado"] = None if out["lado"] == "L" else "R"
    out["pos_op"] = bool(re.search(r"pos[- ]?op|adjuvant|leito|prostatectomia|mastectomia|histerectomia|cupula|quadrantectomia|setorectomia|"
                                     r"ressecad|ressecc|operad", t))
    out["sib"] = len(out["doses_gy"]) > 1 or bool(re.search(r"\bsib\b|boost integrado|simultaneo|integrado", t))
    return out


# ---------------------------------------------------------------------------------------------------- estruturas
def dose_do_nome(nome: str) -> float | None:
    """PTV_70 → 70; PTV5040 → 50,4; PTV_45Gy → 45; PTV_4500cGy → 45; PTV-63 → 63."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(c?gy)?", nome[3:] if PADRAO_ALVO.match(nome) else nome, re.IGNORECASE)
    if not m:
        return None
    v = _num(m.group(1))
    if (m.group(2) or "").lower() == "cgy" or v >= 1000:
        v /= 100.0
    return v if 10.0 <= v <= 90.0 else None


def _lado_do_alvo(alvo: np.ndarray, corpo: np.ndarray, x: np.ndarray) -> str:
    """'L' ou 'R' (HFS: esquerda do paciente = +x)."""
    xa = x[np.nonzero(alvo)[2]].mean()
    xc = x[np.nonzero(corpo)[2]].mean()
    return "L" if xa > xc else "R"


def _tg_para_estrutura(nomes: list[str]) -> dict[str, str]:
    out = {}
    for n in nomes:
        tg = nome_tg263(n)
        if tg and tg != "External" and tg not in out:
            out[tg] = n
    return out


def _converter(nome: str, c: dict, vol_cc: float) -> dict | None:
    m, val = str(c["metrica"]), float(c["valor"])
    base = {"estrutura": nome, "volume_pct": None, "peso": float(c.get("peso", 1.0)),
            "origem": f"{m} ≤ {val:g}" + (" Gy" if m.startswith("D") else (" cm³" if m.endswith("_cc") else " %"))}
    if m == "Dmax" or re.fullmatch(r"D[\d.]+cc", m):
        return {**base, "tipo": "max", "dose_gy": round(FOLGA_MAX * val, 2)}
    if m == "Dmean":
        return {**base, "tipo": "media", "dose_gy": round(FOLGA_MEDIA * val, 2)}
    mm = re.fullmatch(r"V([\d.]+)Gy(_cc)?", m)
    if mm:
        pct = val if not mm.group(2) else (100.0 * val / vol_cc if vol_cc > 0 else None)
        if pct is None:
            return None
        if pct >= 100:
            return None                                   # o órgão inteiro cabe no limite: nada a otimizar
        return {**base, "tipo": "dvh", "dose_gy": float(mm.group(1)), "volume_pct": round(FOLGA_VOL * pct, 1)}
    return None


def analisar(estruturas: dict, grade, corpo: np.ndarray, voxel_cc: float, texto: str = "", sitio: str | None = None,
             prescricao_gy: float | None = None, fracoes: int | None = None, alvo_prescricao: str | None = None,
             ia_disponivel: bool = False) -> dict:
    """`estruturas`: nome → máscara (na grade de dose). Devolve objetivos, o que criar, o que falta e a explicação."""
    sitios = carregar()
    leitura = interpretar(texto, sitios) if texto.strip() else {"sitio": None, "doses_gy": [], "fracoes": None,
                                                                   "lado": None, "pos_op": False, "sib": False,
                                                                   "termos": [], "dose_fracao_gy": None}
    exp, avisos = [], []
    nomes = [n for n, m in estruturas.items() if m.any()]
    tg = _tg_para_estrutura(nomes)

    # --- alvos e doses
    cand = [n for n in nomes if PADRAO_ALVO.match(n)]
    ptvs = [n for n in cand if n.upper().startswith("PTV")] or [n for n in cand if n.upper().startswith(("CTV", "ITV"))]
    alvos = []
    for n in ptvs:
        alvos.append([n, dose_do_nome(n)])
    doses_txt = sorted(leitura["doses_gy"], reverse=True)
    if alvo_prescricao and prescricao_gy:
        for a in alvos:
            if a[0] == alvo_prescricao and a[1] is None:
                a[1] = prescricao_gy
                exp.append(f"{a[0]}: dose da prescrição do plano ({prescricao_gy:g} Gy)")
        if doses_txt and all(abs(d - prescricao_gy) > 0.05 for d in doses_txt):
            avisos.append(f"A descrição fala em {', '.join(f'{d:g}' for d in doses_txt)} Gy, mas a prescrição do plano "
                          f"é {prescricao_gy:g} Gy: os objetivos seguem a prescrição (ajuste-a se for o caso)")
            doses_txt = []
        doses_txt = [d for d in doses_txt if abs(d - prescricao_gy) > 0.05]
    sem = [a for a in alvos if a[1] is None]
    if sem and doses_txt:
        # sem dose no nome: as doses da descrição, a maior para o alvo menor (boost)
        sem.sort(key=lambda a: int(estruturas[a[0]].sum()))
        for a, d in zip(sem, doses_txt):
            a[1] = d
            exp.append(f"{a[0]}: dose {d:g} Gy tirada da descrição (o alvo menor recebe a dose maior)")
    for a in alvos:
        if a[1] is None and len(alvos) == 1 and prescricao_gy:
            a[1] = prescricao_gy
            exp.append(f"{a[0]}: único PTV, recebe a prescrição ({prescricao_gy:g} Gy)")
    sem = [a[0] for a in alvos if a[1] is None]
    if sem:
        avisos.append("Sem dose para " + ", ".join(sem) + ": ponha a dose no nome (ex.: PTV_45) ou na descrição")
    alvos = [a for a in alvos if a[1] is not None]
    if not alvos:
        avisos.append("Nenhum PTV com dose reconhecido: defina o alvo na tabela")
    alvos.sort(key=lambda a: -a[1])
    for n, d in alvos:
        exp.append(f"Alvo {n}: {d:g} Gy ({estruturas[n].sum() * voxel_cc:.0f} cm³)")
    uniao = np.zeros_like(corpo, dtype=bool)
    for n, _ in alvos:
        uniao |= estruturas[n]

    # --- lado e órgãos perto do alvo
    x = np.asarray(grade.x)
    lado = leitura["lado"]
    if uniao.any() and corpo.any():
        lado_ct = _lado_do_alvo(uniao, corpo, x)
        if lado is None:
            lado = lado_ct
            exp.append(f"Lado do alvo pela TC: {'esquerdo' if lado == 'L' else 'direito'}")
        elif lado != lado_ct:
            avisos.append(f"A descrição diz {'esquerdo' if lado == 'L' else 'direito'}, mas o alvo está mais à "
                          f"{'esquerda' if lado_ct == 'L' else 'direita'} do paciente na TC: confira")
    perto = []
    if uniao.any():
        from scipy.ndimage import distance_transform_edt
        passo = (abs(grade.z[1] - grade.z[0]), abs(grade.y[1] - grade.y[0]), abs(grade.x[1] - grade.x[0]))
        dist = distance_transform_edt(~uniao, sampling=passo)
        for t_, n in tg.items():
            m = estruturas[n]
            dmin = float(dist[m].min())
            dentro = float((m & uniao).sum() / max(m.sum(), 1) * 100)
            perto.append((dmin, t_, n, dentro))
        perto.sort()
        if corpo.any():
            pele = distance_transform_edt(corpo, sampling=passo)
            dpele = float(pele[uniao].min())
            if dpele < 5.0:
                avisos.append(f"O alvo chega a {dpele:.0f} mm da pele: use o flash (VMAT: 'margem de flash') e/ou "
                              f"bolus; a otimização não deve tentar dar dose no ar")

    # --- sítio
    escolhido = sitio if sitio in sitios else None
    if escolhido:
        exp.append(f"Protocolo escolhido: {sitios[escolhido]['nome']}")
    elif leitura["sitio"]:
        escolhido = leitura["sitio"]
        exp.append(f"Sítio pela descrição ({', '.join(leitura['termos'])}): {sitios[escolhido]['nome']}")
    else:
        for dmin, t_, n, _ in perto:
            if t_ in ORGAO_SITIO and ORGAO_SITIO[t_] in sitios and dmin < 30:
                escolhido = ORGAO_SITIO[t_]
                exp.append(f"Sítio pela anatomia (órgão mais perto do alvo: {n}, {dmin:.0f} mm): "
                           f"{sitios[escolhido]['nome']} — confira")
                break
    if not escolhido:
        avisos.append("Não reconheci o sítio: escolha o protocolo na lista")
        return {"sitio": None, "leitura": leitura, "alvos": [{"estrutura": n, "dose_gy": d} for n, d in alvos],
                "objetivos": [{"estrutura": n, "tipo": "alvo", "dose_gy": round(d, 2), "volume_pct": None,
                               "peso": 5.0, "origem": "alvo"} for n, d in alvos],
                "criar": [], "faltando": [], "explicacao": exp, "avisos": avisos, "parametros": {}}
    S = sitios[escolhido]

    # --- fracionamento
    dfx = leitura.get("dose_fracao_gy")
    if dfx is None and leitura["doses_gy"] and leitura["fracoes"]:
        dfx = max(leitura["doses_gy"]) / leitura["fracoes"]
    if dfx is None and prescricao_gy and fracoes:
        dfx = prescricao_gy / fracoes
    ref = S.get("dose_fracao_ref")
    if dfx and ref and abs(dfx - ref) > 0.3:
        avisos.append(f"Fracionamento de {dfx:.2f} Gy/fração, mas os limites do protocolo são para {ref:g} Gy/fração "
                      f"(hipofracionado): revise os valores (não há conversão automática)")

    # --- objetivos
    sub = {"ipsi": lado or "L", "contra": "R" if (lado or "L") == "L" else "L"}
    objetivos = [{"estrutura": n, "tipo": "alvo", "dose_gy": round(d, 2), "volume_pct": None, "peso": 5.0,
                  "origem": "alvo"} for n, d in alvos]
    criar, faltando, vistos = [], [], set()
    for chave, lims in (S.get("orgaos") or {}).items():
        t_ = chave.format(**sub)
        nome = tg.get(t_)
        if nome is None and t_ in BILATERAIS:
            e, d, novo = BILATERAIS[t_]
            if e in tg and d in tg:
                nome = novo if novo not in estruturas else novo + "_TPS"
                criar.append({"nome": nome, "operacao": "uniao", "a": tg[e], "b": tg[d]})
                vol = float((estruturas[tg[e]] | estruturas[tg[d]]).sum() * voxel_cc)
                exp.append(f"{novo}: criada como união de {tg[e]} e {tg[d]} para os limites dos dois pulmões/rins")
            else:
                continue
        elif nome is None:
            continue
        else:
            vol = float(estruturas[nome].sum() * voxel_cc)
        for c in lims or []:
            o = _converter(nome, c, vol)
            if o and alvos and o["dose_gy"] >= alvos[0][1] * 1.02:
                continue
            if o and (nome, o["tipo"], o["dose_gy"]) not in vistos:
                vistos.add((nome, o["tipo"], o["dose_gy"]))
                objetivos.append(o)
    for ess in S.get("essenciais") or []:
        t_ = ess.format(**sub)
        if t_ in tg or (t_ in BILATERAIS and all(x in tg for x in BILATERAIS[t_][:2])):
            continue
        faltando.append({"tg": t_, "ia": TG_PARA_IA.get(t_), "ia_disponivel": bool(ia_disponivel and TG_PARA_IA.get(t_))})
    if faltando:
        txt = ", ".join(f["tg"] for f in faltando)
        avisos.append(f"Faltam órgãos do protocolo: {txt}" + (" — dá para contornar por IA" if ia_disponivel and
                                                             any(f["ia"] for f in faltando) else ""))
    for dmin, t_, n, dentro in perto[:6]:
        if dentro >= 5:
            exp.append(f"{n}: {dentro:.0f}% dentro do PTV — o objetivo vale só para a parte fora do alvo")
        elif dmin < 10:
            exp.append(f"{n}: a {dmin:.0f} mm do alvo")
    par = {"arcos": int(S.get("arcos", 2)), "arco_parcial": bool(S.get("arco_parcial", False)), "lado": lado}
    if par["arco_parcial"] and lado:
        # arcos do lado do alvo (HFS): esquerdo → 300°→160° passando pelo 0; direito → 200°→60°
        par["inicio"], par["fim"] = (300.0, 160.0) if lado == "L" else (200.0, 60.0)
        exp.append("Arcos parciais do lado do alvo, para poupar o pulmão/mama contralaterais")
    return {"sitio": escolhido, "sitio_nome": S.get("nome"), "fonte": S.get("fonte"), "leitura": leitura,
            "alvos": [{"estrutura": n, "dose_gy": d} for n, d in alvos], "objetivos": objetivos, "criar": criar,
            "faltando": faltando, "explicacao": exp, "avisos": avisos, "parametros": par}
