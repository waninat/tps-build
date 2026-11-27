"""Perfil de planejamento: planejar "como no Monaco" ou "como no Eclipse". SRS-PLN-120.

O perfil muda três coisas, sem mexer no motor de cálculo nem na entregabilidade (que é a da máquina):

1. Vocabulário dos objetivos — a tela mostra as funções do sistema escolhido (Monaco: Target Penalty, Quadratic
   Overdose, Serial, Parallel…; Eclipse: Upper, Lower, Mean, gEUD, NTO, com prioridades 0–1000) e cada uma é
   traduzida para a função de custo equivalente do otimizador do TPS.
2. Padrões da otimização e do sequenciamento — os valores típicos de cada sistema para Elekta step-and-shoot
   (níveis de intensidade, UM mínima e área mínima de segmento, suavidade da fluência, NTO).
3. Exportação — a ficha de objetivos que acompanha o plano sai no formato do sistema (Monaco: tabela de funções de
   custo; Eclipse: modelo de objetivos XML + tabela), e o RT Plan segue as convenções desse sistema na descrição dos
   feixes e no rótulo. O fabricante gravado no DICOM continua sendo o TPS (a origem do plano não é falseada).

As equivalências são aproximadas (as funções de custo de cada fabricante não são públicas em detalhe): servem para
o plano chegar no outro sistema já com os objetivos montados, prontos para conferir e reotimizar.
"""
from __future__ import annotations

import csv
import datetime as dt
from pathlib import Path
from xml.sax.saxutils import quoteattr

# ----------------------------------------------------------------------------------------------- vocabulário
# campos: dose (Gy) | volume (%) | k (expoente) | prioridade (Eclipse) | peso (Monaco: relativo) | restrito
FUNCOES = {
    "monaco": [
        {"funcao": "Target Penalty", "tipo": "alvo", "campos": ["dose", "volume"], "alvo": True,
         "ajuda": "Prescrição com volume mínimo coberto (Minimum Volume %)."},
        {"funcao": "Quadratic Underdose", "tipo": "min", "campos": ["dose"], "alvo": True,
         "ajuda": "Penaliza dose abaixo do limite (RMS)."},
        {"funcao": "Quadratic Overdose", "tipo": "max", "campos": ["dose", "peso", "restrito"],
         "ajuda": "Penaliza dose acima do limite (RMS). Em alvo: controla o ponto quente."},
        {"funcao": "Maximum Dose", "tipo": "max", "campos": ["dose"], "restrito_fixo": True,
         "ajuda": "Dose máxima rígida (constrained)."},
        {"funcao": "Overdose DVH", "tipo": "dvh", "campos": ["dose", "volume", "peso", "restrito"],
         "ajuda": "No máximo Volume % acima da dose."},
        {"funcao": "Underdose DVH", "tipo": "dvh_min", "campos": ["dose", "volume"], "alvo": True,
         "ajuda": "Pelo menos Volume % com a dose."},
        {"funcao": "Serial", "tipo": "serial", "campos": ["dose", "k", "peso", "restrito"],
         "ajuda": "EUD com expoente k alto (órgão serial: medula, tronco). k = 1 equivale à média."},
        {"funcao": "Parallel", "tipo": "paralelo", "campos": ["dose", "volume", "peso", "restrito"],
         "ajuda": "Dano médio do órgão (Mean Organ Damage %) acima da dose de referência."},
    ],
    "eclipse": [
        {"funcao": "Upper", "tipo": "max_ou_dvh", "campos": ["dose", "volume", "prioridade"],
         "ajuda": "No máximo Volume % acima da dose (0% = dose máxima)."},
        {"funcao": "Lower", "tipo": "dvh_min", "campos": ["dose", "volume", "prioridade"], "alvo": True,
         "ajuda": "Pelo menos Volume % com a dose (100% = dose mínima). No alvo, com prioridade alta."},
        {"funcao": "Mean", "tipo": "media", "campos": ["dose", "prioridade"], "ajuda": "Dose média máxima."},
        {"funcao": "Upper gEUD", "tipo": "serial", "campos": ["dose", "k", "prioridade"],
         "ajuda": "gEUD máximo com parâmetro a (= k; alto para órgão serial)."},
        {"funcao": "Target gEUD", "tipo": "alvo", "campos": ["dose", "prioridade"], "alvo": True,
         "ajuda": "Dose uniforme no alvo (vira o objetivo de alvo do TPS)."},
    ],
}
FUNCOES["tps"] = [{"funcao": t, "tipo": t, "campos": ["dose", "volume", "peso", "restrito"]}
                  for t in ("alvo", "min", "max", "media", "dvh", "dvh_min", "serial", "paralelo")]

PERFIS = {
    "tps": {"rotulo": "TPS (nativo)", "sistema": "TPS",
            "otimizacao": {}},
    "monaco": {"rotulo": "Como no Monaco", "sistema": "Monaco",
               # Monaco (Elekta step-and-shoot): área mínima de segmento 2 cm², largura mínima 0,5 cm, UM mínima
               # por segmento 2, suavidade da fluência "média", sem NTO (o Monaco usa Quadratic Overdose no corpo)
               "otimizacao": {"niveis": 8, "um_min": 2.0, "area_min_cm2": 2.0, "peso_nto": 0.5,
                              "suavidade": 0.05, "forma_segmentos": True},
               "nto": None},
    "eclipse": {"rotulo": "Como no Eclipse", "sistema": "Eclipse",
                # Eclipse (PO + Leaf Motion Calculator sMLC): NTO automático (prioridade 100, 0,3 cm da borda,
                # 105% → 60%, queda 0,05), suavização X 40 / Y 30, ~10 níveis de intensidade, UM mínima 3
                "otimizacao": {"niveis": 10, "um_min": 3.0, "area_min_cm2": 3.0, "peso_nto": 1.5,
                               "suavidade": 0.08, "forma_segmentos": False},
                "nto": {"prioridade": 100, "distancia_cm": 0.3, "inicio_pct": 105, "fim_pct": 60, "queda": 0.05}},
}


def perfis() -> dict:
    return {k: {"rotulo": v["rotulo"], "sistema": v["sistema"], "funcoes": FUNCOES[k],
                "otimizacao": v["otimizacao"], "nto": v.get("nto")} for k, v in PERFIS.items()}


def _perfil(perfil: str | None) -> str:
    p = (perfil or "tps").lower()
    if p not in PERFIS:
        raise ValueError(f"Perfil de planejamento desconhecido: {perfil}")
    return p


def _num(v, padrao=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return padrao


def peso_de_prioridade(prioridade: float) -> float:
    """Prioridade do Eclipse (0–1000) → peso relativo do TPS. 100 → 1; o alvo costuma ter 100–150."""
    return max(0.0, float(prioridade)) / 100.0


def prioridade_de_peso(peso: float) -> int:
    return int(round(min(1000.0, max(0.0, float(peso) * 100.0))))


def para_interno(perfil: str, linha: dict) -> dict:
    """Uma linha de objetivo no vocabulário do sistema → objetivo do TPS {estrutura, tipo, dose_gy, volume_pct, peso,
    restrito}. Linha sem "funcao" já está no formato do TPS."""
    perfil = _perfil(perfil)
    if not linha.get("funcao"):
        return dict(linha)
    f = next((x for x in FUNCOES[perfil] if x["funcao"].lower() == str(linha["funcao"]).lower()), None)
    if f is None:
        raise ValueError(f"Função {linha['funcao']} não existe no perfil {PERFIS[perfil]['sistema']}")
    dose = _num(linha.get("dose_gy"))
    vol = _num(linha.get("volume_pct"))
    k = _num(linha.get("k"))
    peso = peso_de_prioridade(_num(linha.get("prioridade"), 100)) if perfil == "eclipse" \
        else _num(linha.get("peso"), 1.0)
    tipo = f["tipo"]
    if tipo == "max_ou_dvh":                     # Upper: 0% = máximo; >0% = ponto do DVH
        tipo = "max" if not vol else "dvh"
    if tipo == "dvh_min" and vol is not None and vol >= 99.9:
        tipo = "min"                              # Lower 100% = dose mínima
    if tipo == "alvo" and perfil == "monaco" and vol is not None and vol < 90:
        tipo = "dvh_min"
    out = {"estrutura": linha.get("estrutura"), "tipo": tipo, "dose_gy": dose, "peso": peso,
           "restrito": bool(linha.get("restrito") or f.get("restrito_fixo")),
           "volume_pct": k if tipo == "serial" else (vol if tipo in ("dvh", "dvh_min", "paralelo") else None),
           "funcao": f["funcao"], "perfil": perfil}
    if perfil == "eclipse":
        out["prioridade"] = _num(linha.get("prioridade"), 100)
        # no Eclipse a prioridade alta é a forma de "exigir": ≥ 300 num órgão conta como restrição
        out["restrito"] = bool(out["prioridade"] >= 300 and tipo not in ("alvo", "min", "dvh_min"))
    return out


def de_interno(perfil: str, o: dict) -> list[dict]:
    """Objetivo do TPS → linha(s) no vocabulário do sistema (para mostrar e exportar)."""
    perfil = _perfil(perfil)
    t, d, v = o.get("tipo"), _num(o.get("dose_gy")), _num(o.get("volume_pct"))
    peso = _num(o.get("peso"), 1.0)
    base = {"estrutura": o.get("estrutura"), "dose_gy": d}
    if perfil == "tps":
        return [dict(o)]
    if perfil == "eclipse":
        pr = _num(o.get("prioridade")) or prioridade_de_peso(peso * (3 if o.get("restrito") else 1))
        if t == "alvo":
            return [{**base, "funcao": "Lower", "volume_pct": 100.0, "dose_gy": round(0.95 * d, 3), "prioridade": max(pr, 120)},
                    {**base, "funcao": "Lower", "volume_pct": 95.0, "prioridade": max(pr, 120)},
                    {**base, "funcao": "Upper", "volume_pct": 0.0, "dose_gy": round(1.07 * d, 3), "prioridade": max(pr, 100)}]
        mapa = {"min": ("Lower", 100.0), "max": ("Upper", 0.0), "dvh": ("Upper", v), "dvh_min": ("Lower", v),
                "media": ("Mean", None), "serial": ("Upper gEUD", None), "paralelo": ("Upper", v)}
        f, vol = mapa.get(t, ("Upper", v))
        r = {**base, "funcao": f, "volume_pct": vol, "prioridade": pr}
        if t == "serial":
            r["k"] = v or 8.0
        return [r]
    # Monaco
    if t == "alvo":
        return [{**base, "funcao": "Target Penalty", "volume_pct": 95.0},
                {**base, "funcao": "Quadratic Overdose", "dose_gy": round(1.07 * d, 3), "peso": 1.0, "restrito": True}]
    mapa = {"min": "Quadratic Underdose", "max": "Maximum Dose" if o.get("restrito") else "Quadratic Overdose",
            "dvh": "Overdose DVH", "dvh_min": "Underdose DVH", "media": "Serial", "serial": "Serial",
            "paralelo": "Parallel"}
    r = {**base, "funcao": mapa.get(t, "Quadratic Overdose"), "peso": peso, "restrito": bool(o.get("restrito"))}
    if t in ("dvh", "dvh_min", "paralelo"):
        r["volume_pct"] = v
    if t in ("media", "serial"):
        r["k"] = 1.0 if t == "media" else (v or 8.0)
    return [r]


def opcoes_otimizacao(perfil: str | None, pedido: dict) -> dict:
    """Padrões do perfil sob o que o planejador escolheu na tela (o pedido sempre vence)."""
    base = dict(PERFIS[_perfil(perfil)]["otimizacao"])
    base.update({k: v for k, v in (pedido or {}).items() if v not in (None, "")})
    return base


# ----------------------------------------------------------------------------------------------- exportação
def _linha_csv(r: dict) -> list:
    return [r.get("estrutura", ""), r.get("funcao", ""), "" if r.get("dose_gy") is None else f"{r['dose_gy']:g}",
            "" if r.get("volume_pct") is None else f"{r['volume_pct']:g}", "" if r.get("k") is None else f"{r['k']:g}",
            r.get("prioridade", r.get("peso", "")), "sim" if r.get("restrito") else ""]


def gravar_ficha(perfil: str, objetivos: list[dict], destino_dir: str | Path, base: str, cabecalho: str = "",
                 sitio: str = "") -> list[Path]:
    """Arquivos de objetivos no formato do sistema do perfil. Devolve os caminhos gravados."""
    perfil = _perfil(perfil)
    d = Path(destino_dir)
    out = []
    if perfil != "eclipse":
        from tps.exportacao.objetivos_monaco import gravar
        return [gravar(objetivos, d / f"OBJ_Monaco_{base}.csv", cabecalho)]
    linhas = [r for o in objetivos for r in de_interno("eclipse", o)]
    p = d / f"OBJ_Eclipse_{base}.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        if cabecalho:
            w.writerow([cabecalho])
        w.writerow(["Estrutura", "Objetivo (Eclipse)", "Dose (Gy)", "Volume (%)", "a (gEUD)", "Prioridade", "Exigido"])
        for r in linhas:
            w.writerow(_linha_csv(r))
        nto = PERFIS["eclipse"]["nto"]
        w.writerow(["NTO", "Normal Tissue Objective (automático)", "", "", "", nto["prioridade"],
                    f"distância {nto['distancia_cm']} cm, {nto['inicio_pct']}% → {nto['fim_pct']}%, queda {nto['queda']}"])
    out.append(p)
    out.append(gravar_modelo_eclipse(linhas, d / f"OBJ_Eclipse_{base}.xml", sitio or base))
    return out


def gravar_modelo_eclipse(linhas: list[dict], destino: str | Path, nome: str) -> Path:
    """Modelo de objetivos de otimização no formato XML de "Objective Template" do Eclipse (importar pelo gerenciador
    de modelos). O esquema muda entre versões do Eclipse: se a importação recusar, use o CSV ao lado e, de preferência,
    envie um modelo exportado do seu Eclipse para ajustarmos o formato."""
    estruturas: dict[str, list] = {}
    for r in linhas:
        estruturas.setdefault(r.get("estrutura") or "", []).append(r)
    op = {"Upper": "Upper", "Lower": "Lower", "Mean": "Upper", "Upper gEUD": "Upper"}
    tipo = {"Upper": "PointObjective", "Lower": "PointObjective", "Mean": "MeanDoseObjective",
            "Upper gEUD": "gEUDObjective"}
    nto = PERFIS["eclipse"]["nto"]
    x = ['<?xml version="1.0" encoding="utf-8"?>',
         '<ObjectiveTemplate xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" Version="1.1">',
         f'  <Preview ID={quoteattr(nome[:16])} Type="Objective" ApprovalStatus="Unapproved" TreatmentSite="" '
         f'AssignedUsers="" Description="Exportado pelo TPS (perfil Eclipse)" '
         f'LastModified="{dt.datetime.now().strftime("%Y-%m-%d %H:%M")}" />',
         '  <Type>IMRT</Type>',
         '  <Helpers>',
         f'    <NormalTissueObjective Use="true" Priority="{nto["prioridade"]}" DistanceFromTargetBorder='
         f'"{nto["distancia_cm"]}" StartDose="{nto["inicio_pct"]}" EndDose="{nto["fim_pct"]}" '
         f'FallOff="{nto["queda"]}" Auto="false" />',
         '  </Helpers>',
         '  <ObjectivesAllStructures>']
    for est, rs in estruturas.items():
        x.append(f'    <ObjectivesOneStructure ID={quoteattr(est)} NAME={quoteattr(est)} SurfaceOnly="false">')
        x.append('      <Objectives>')
        for r in rs:
            dose = r.get("dose_gy") or 0.0
            vol = 0.0 if r.get("volume_pct") is None else r["volume_pct"]
            a = f' ParameterA="{r.get("k") or 1:g}"' if r["funcao"] == "Upper gEUD" else ""
            x.append(f'        <Objective Type="{tipo[r["funcao"]]}" Operator="{op[r["funcao"]]}" '
                     f'Dose="{100 * dose:g}" DoseUnit="cGy" Volume="{vol:g}" Priority="{int(r.get("prioridade", 100))}" '
                     f'Group="0"{a} />')
        x.append('      </Objectives>')
        x.append('    </ObjectivesOneStructure>')
    x += ['  </ObjectivesAllStructures>', '</ObjectiveTemplate>', '']
    p = Path(destino)
    p.write_text("\n".join(x), encoding="utf-8")
    return p


def convencoes_exportacao(perfil: str | None) -> dict:
    """Convenções do RT Plan de cada sistema (o que muda no arquivo; a geometria e as UM são as mesmas)."""
    perfil = _perfil(perfil)
    if perfil == "eclipse":
        # Eclipse: nome do feixe = ID do campo, descrição vazia ou com a técnica; rótulo do plano até 13 caracteres
        # (o "Plan ID" do Eclipse); coeficiente de dose de referência acumulado em cada ponto de controle
        return {"sistema": "Eclipse", "rotulo_max": 13, "descricao_feixe": "tecnica",
                "coef_dose_referencia": True}
    return {"sistema": "Monaco" if perfil == "monaco" else "TPS", "rotulo_max": 16, "descricao_feixe": "campo",
            "coef_dose_referencia": False}
