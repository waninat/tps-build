"""Copiloto do planejamento com o Claude: conversa sobre o plano aberto e usa as ferramentas do TPS. SRS-PLN-130.

Além de sugerir objetivos, o Claude pode ler o plano e a dose (DVH, restrições, conferência de UM), dar um parecer
técnico com sugestões, e AGIR no rascunho: mudar objetivos e reotimizar, criar/alterar/remover campos e sub-campos,
blindar um órgão num campo, mover lâminas, calcular com field-in-field/compensador/proteção de órgãos, pós-processar
contornos e montar o rascunho de uma máquina nova (comissionamento guiado: pede os dados que faltam).

Limites (as regras do TPS valem para o agente como para qualquer usuário):
  • só no plano em RASCUNHO; o agente não aprova, não desaprova, não exporta, não envia e não apaga planos;
  • antes da primeira alteração de cada pedido o TPS guarda o estado do plano (botão "Desfazer o copiloto");
  • cada ação passa pela validação normal do TPS (mecânica do MLC, limites da máquina) e entra no histórico do plano;
  • o que sai da clínica é o mesmo resumo anônimo do assistente (sem imagens, sem nome/ID do paciente); a conversa
    fica registrada em <dados>/ia/claude/.
A responsabilidade pelo plano continua sendo do físico/planejador: o parecer do Claude é uma segunda opinião.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import time
from pathlib import Path

import numpy as np
import yaml

from tps import paths
from tps.planejamento import assistente_claude as C

MAX_VOLTAS = 14
ACOES = {"criar_plano", "criar_campos", "definir_objetivos", "otimizar_modulado", "calcular", "salvar_campo", "remover_campo", "bloquear_orgao",
         "mover_laminas", "pos_processar_contorno", "criar_rascunho_maquina", "definir_perfil"}

SISTEMA = """Você é o copiloto de um físico médico num sistema de planejamento (TPS) para aceleradores Elekta (Precise
com MLCi: 40 pares de 1 cm, sem interdigitação; Synergy com Agility: 80 pares de 0,5 cm). Você conversa em português,
de forma objetiva, e usa as ferramentas para ler e alterar o plano aberto. Você NÃO vê as imagens: só o que as
ferramentas devolvem (resumo anônimo, DVH, medidas, campos).

Como trabalhar:
- Antes de opinar sobre um plano, leia-o (ler_plano, ler_dose) — não invente números.
- Você PODE fazer o plano do zero: listar_opcoes → criar_plano (prescrição, alvo, máquina) → criar_campos (3D com
  ângulos, modelo do sítio, IMRT, VMAT ou DCA) → objetivos/otimização ou calcular → ler_dose → ajustes. Não espere
  o usuário criar o plano: se ele pedir um plano, faça.
- Ao avaliar: compare com a prescrição e as restrições (ler_dose traz a avaliação QUANTEC/clínica), cobertura do
  alvo (D95/D98, V95), homogeneidade (HI, D2/Dmax), órgãos; dê um parecer com dar_parecer (aval: "aceitavel",
  "melhorar" ou "refazer") e sugestões concretas e priorizadas (objetivos novos, ângulos, blindagens, técnica).
- Para agir, mude o mínimo necessário e explique o porquê. Depois de alterar campos ou objetivos, recalcule
  (calcular ou otimizar_modulado) e confira o efeito (ler_dose) antes de dizer que melhorou.
- 3D: para proteger um órgão você pode blindá-lo num campo (bloquear_orgao; no meio do par o TPS divide o campo em
  segmentos), criar sub-campos/campos novos (salvar_campo com campo_pai não existe — use proteger_oars no calcular
  para sub-campos automáticos), mover lâminas (mover_laminas), mudar pesos (calcular com otimizar_pesos).
- IMRT/VMAT: objetivos com definir_objetivos (tipos: alvo, min, max, media, dvh, dvh_min, serial, paralelo; doses
  TOTAIS em Gy) e otimizar_modulado. A otimização demora minutos: só rode quando fizer sentido.
- Comissionamento de máquina nova: chame checklist_comissionamento, peça ao usuário os dados que faltam (um bloco
  por vez: geometria e MLC → nomes DICOM → energias → dados de feixe) e só então criar_rascunho_maquina. O rascunho
  não entra em uso: a física confere, mede o que falta e roda o comissionamento do modelo.
- Você não aprova nem exporta planos e não toma decisões clínicas: aponte o que o físico precisa decidir.
- Respostas curtas; números com unidade; use listas quando ajudar.
"""


def _esquema(props: dict, req: list | None = None) -> dict:
    return {"type": "object", "properties": props, "required": req or []}


_NUM = {"type": "number"}
_STR = {"type": "string"}
FERRAMENTAS = [
    {"name": "ler_plano", "description": "Plano aberto: prescrição, técnica, perfil, estado, campos (gantry, "
     "colimador, mesa, UM, peso, segmentos, sub-campos, blindagens), estruturas (volume, distância ao alvo).",
     "input_schema": _esquema({})},
    {"name": "ler_dose", "description": "Resultado do último cálculo: estatísticas por estrutura (Dmean, Dmax, D2, "
     "D50, D95, D98, V95/V100/V107 no alvo), avaliação das restrições, HI, Dmax global e conferência de UM.",
     "input_schema": _esquema({})},
    {"name": "dvh", "description": "DVH de uma estrutura: V(dose) e D(volume) pedidos.",
     "input_schema": _esquema({"estrutura": _STR, "doses_gy": {"type": "array", "items": _NUM},
                               "volumes_pct": {"type": "array", "items": _NUM}}, ["estrutura"])},
    {"name": "medir", "description": "Distância mínima entre duas estruturas, sobreposição e posição relativa.",
     "input_schema": _esquema({"a": _STR, "b": _STR}, ["a", "b"])},
    {"name": "ler_campo", "description": "Um campo: mandíbulas, lâminas por par (mm no isocentro), segmentos, UM.",
     "input_schema": _esquema({"nome": _STR}, ["nome"])},
    {"name": "ler_objetivos", "description": "Objetivos de otimização atuais do plano modulado.",
     "input_schema": _esquema({})},
    {"name": "ler_maquina", "description": "Máquina do plano: MLC, limites mecânicos, energias, técnicas e o que "
     "ainda está marcado CONFIRMAR.", "input_schema": _esquema({})},
    {"name": "ler_protocolo", "description": "Limites de dose do protocolo de um sítio.",
     "input_schema": _esquema({"sitio": _STR}, ["sitio"])},
    {"name": "listar_opcoes", "description": "Máquinas (energias), modelos de plano 3D por sítio e planos já abertos "
     "na sessão — use antes de criar um plano.", "input_schema": _esquema({})},
    {"name": "criar_plano", "description": "Cria um plano NOVO (rascunho) com a prescrição. O isocentro vai para o "
     "centro do alvo. Depois crie os campos com criar_campos.",
     "input_schema": _esquema({"rotulo": _STR, "dose_total_gy": _NUM, "fracoes": {"type": "integer"}, "alvo": _STR,
                               "maquina": _STR, "energia": _STR,
                               "normalizacao": {"type": "string", "enum": ["media_alvo", "cobertura", "ponto", "isocentro"]}},
                              ["rotulo", "dose_total_gy", "fracoes", "alvo"])},
    {"name": "criar_campos", "description": "Cria/substitui os campos do plano atual. tecnica: '3D' (gantries: lista "
     "de ângulos, ex. [0,90,180,270]; conformados ao alvo com margem_mm e blindagens), 'modelo' (modelo 3D do sítio: "
     "use o id de listar_opcoes; refaz o plano com a prescrição dele), 'IMRT' (n campos equiespaçados), 'VMAT' "
     "(n_arcos) ou 'DCA' (arco conformacional).",
     "input_schema": _esquema({"tecnica": {"type": "string", "enum": ["3D", "modelo", "IMRT", "VMAT", "DCA"]},
                               "gantries": {"type": "array", "items": _NUM}, "colimador": _NUM,
                               "margem_mm": _NUM, "blindagens": {"type": "array", "items": _esquema(
                                   {"nome": _STR, "margem": _NUM}, ["nome"])},
                               "otimizar_colimador": {"type": "boolean"}, "n": {"type": "integer"},
                               "n_arcos": {"type": "integer"}, "modelo": _STR, "alvo": _STR}, ["tecnica"])},
    {"name": "definir_perfil", "description": "Planejar como TPS, Monaco ou Eclipse (vocabulário, padrões e "
     "exportação).", "input_schema": _esquema({"perfil": {"type": "string", "enum": ["tps", "monaco", "eclipse"]}},
                                              ["perfil"])},
    {"name": "definir_objetivos", "description": "Substitui os objetivos do plano modulado (não otimiza).",
     "input_schema": _esquema({"objetivos": {"type": "array", "items": _esquema({
         "estrutura": _STR, "tipo": {"type": "string", "enum": list(C.TIPOS) + ["dvh_min"]}, "dose_gy": _NUM,
         "volume_pct": {"type": ["number", "null"]}, "peso": _NUM, "restrito": {"type": "boolean"}},
         ["estrutura", "tipo", "dose_gy"])}}, ["objetivos"])},
    {"name": "otimizar_modulado", "description": "Roda a otimização IMRT (campos estáticos) ou VMAT (arcos) com os "
     "objetivos atuais e calcula a dose. Demora minutos.",
     "input_schema": _esquema({"tecnica": {"type": "string", "enum": ["IMRT", "VMAT"]},
                               "niveis": {"type": "integer"}, "um_min": _NUM, "area_min_cm2": _NUM,
                               "peso_nto": _NUM, "iteracoes": {"type": "integer"}}, ["tecnica"])},
    {"name": "calcular", "description": "Calcula a dose do plano 3D. Opções: otimizar_pesos, field_in_field, "
     "compensador {niveis}, proteger_oars {oars: [{estrutura, metrica: Dmean|Dmax|V, valor, prioridade}], "
     "subcampos, margem_mm}.",
     "input_schema": _esquema({"otimizar_pesos": {"type": "boolean"}, "field_in_field": {"type": "boolean"},
                               "compensador": {"type": ["object", "null"]}, "proteger_oars": {"type": ["object", "null"]}})},
    {"name": "salvar_campo", "description": "Cria ou altera um campo conformado (nome_antigo para alterar). spec: "
     "nome, gantry, colimador, mesa, peso, alvo, margem (mm ou {x1,x2,y1,y2}), ponto_encontro "
     "(fora|meio|area|dentro), blindagens [{nome, margem}], otimizar_colimador, arco_fim/sentido.",
     "input_schema": _esquema({"spec": {"type": "object"}}, ["spec"])},
    {"name": "remover_campo", "description": "Remove um campo do plano.",
     "input_schema": _esquema({"nome": _STR}, ["nome"])},
    {"name": "bloquear_orgao", "description": "Blinda uma estrutura num campo (refaz a conformação com a blindagem; "
     "prioridade_alvo=true não corta o alvo).",
     "input_schema": _esquema({"campo": _STR, "estrutura": _STR, "margem_mm": _NUM,
                               "prioridade_alvo": {"type": "boolean"}}, ["campo", "estrutura"])},
    {"name": "mover_laminas", "description": "Move lâminas de um campo: pares [{par (1 = primeiro), a_mm, b_mm}] "
     "(mm no isocentro; banco A negativo). A mecânica é conferida.",
     "input_schema": _esquema({"campo": _STR, "pares": {"type": "array", "items": _esquema(
         {"par": {"type": "integer"}, "a_mm": _NUM, "b_mm": _NUM}, ["par"])}}, ["campo", "pares"])},
    {"name": "pos_processar_contorno", "description": "Suaviza/fecha/abre/preenche uma estrutura (como as "
     "ferramentas do Eclipse).", "input_schema": _esquema({"estrutura": _STR, "suavizar_mm": _NUM, "fechar_mm": _NUM,
                                                           "abrir_mm": _NUM, "preencher": {"type": "boolean"},
                                                           "ilhas_cc": _NUM}, ["estrutura"])},
    {"name": "checklist_comissionamento", "description": "Lista dos dados necessários para comissionar uma "
     "máquina nova (geometria, MLC, DICOM, energias, dados de feixe), com o que cada um alimenta.",
     "input_schema": _esquema({})},
    {"name": "criar_rascunho_maquina", "description": "Grava o rascunho de uma máquina nova com os dados informados "
     "pelo usuário (campos ausentes ficam CONFIRMAR). Não entra em uso até a física comissionar.",
     "input_schema": _esquema({"dados": {"type": "object"}}, ["dados"])},
    {"name": "dar_parecer", "description": "Parecer técnico sobre o plano (segunda opinião, não aprovação).",
     "input_schema": _esquema({"aval": {"type": "string", "enum": ["aceitavel", "melhorar", "refazer"]},
                               "resumo": _STR, "pontos": {"type": "array", "items": _STR},
                               "sugestoes": {"type": "array", "items": _STR},
                               "objetivos_sugeridos": {"type": "array", "items": {"type": "object"}}},
                              ["aval", "resumo"])},
]


# ------------------------------------------------------------------------------------------------ comissionamento
CHECKLIST = [
    {"bloco": "Identificação", "itens": [
        ("id", "identificador curto (ex.: clinica_synergy)", "nome da pasta/arquivo"),
        ("vendor", "fabricante (Elekta)", "regras de mecânica"), ("model", "modelo (Precise, Synergy, Versa HD…)", ""),
        ("serial", "número de série", "rastreabilidade"),
        ("dicom_machine_names", "TreatmentMachineName exatamente como no R&V (Mosaiq)", "exportação aceita no R&V")]},
    {"bloco": "Geometria e colimação", "itens": [
        ("sad_mm", "SAD (mm)", "geometria"), ("mlc", "MLC: MLCi, MLCi2, Agility (ou nº de pares e largura)", "conformação"),
        ("jaw_x", "tem diafragma X? (MLCi sim; Agility não)", "ajuste das mandíbulas"),
        ("max_overtravel_mm", "overtravel máximo das lâminas (mm)", "mecânica"),
        ("min_gap_mm", "gap mínimo entre lâminas opostas (mm)", "mecânica, IMRT"),
        ("interdigitation", "permite interdigitação?", "mecânica"),
        ("distancias_fonte_mm", "distância fonte–dispositivo (ASYMX, ASYMY, MLCX) como o R&V espera", "exportação"),
        ("tolerancia", "tabela de tolerância (rótulo no Mosaiq)", "exportação")]},
    {"bloco": "Energias e técnicas", "itens": [
        ("energias_mv", "energias de fótons (e se FFF)", "um modelo por energia"),
        ("techniques", "técnicas licenciadas (3D, step-and-shoot, VMAT)", "o que o TPS oferece"),
        ("vmat", "VMAT: velocidade máx. do gantry (°/s) e das lâminas (mm/s), taxas de dose", "otimização VMAT")]},
    {"bloco": "Dados de feixe (por energia)", "itens": [
        ("pdd", "PDD em água, SSD 100, campos 3–40 cm (CSV: profundidade mm; campos)", "modelo do feixe (CCC)"),
        ("perfis", "perfis inline/crossline em 5 profundidades (dmax, 5, 10, 20, 30 cm) para 3–40 cm", "penumbra e horns"),
        ("fatores_output", "fatores de saída Scp (e Sc, se houver) para campos 2–40 cm", "UM"),
        ("tmr", "TMR (ou TPR) para a conferência independente de UM", "conferência por tabela"),
        ("calibracao", "cGy/UM na condição de referência (TRS-398: profundidade, SSD/SAD, campo)", "UM absolutas"),
        ("qualidade", "TPR20/10 ou %dd(10)x", "espectro"),
        ("transmissao", "transmissão do MLC e das mandíbulas; DLG/folga de ponta (se medido)", "IMRT/VMAT")]},
]


def checklist() -> dict:
    return {"blocos": [{"bloco": b["bloco"], "itens": [{"campo": c, "pergunta": p, "para": u} for c, p, u in b["itens"]]}
                       for b in CHECKLIST],
            "observacao": "Os dados de feixe entram como arquivos CSV na pasta da energia (mesmo formato das máquinas "
                          "existentes). O rascunho fica em <dados>/maquinas_rascunho e não aparece na lista de "
                          "máquinas até a física comissionar e mover para dados/maquinas."}


def criar_rascunho_maquina(dados: dict) -> dict:
    import re
    mid = re.sub(r"[^a-z0-9_]+", "_", str(dados.get("id") or "").lower()).strip("_")[:40]
    if not mid:
        raise ValueError("Informe o identificador da máquina (id)")
    presets = {"mlci": (40, 10.0, False, True), "mlci2": (40, 10.0, True, True), "agility": (80, 5.0, True, False)}
    mlc = str(dados.get("mlc") or "").lower().replace(" ", "")
    pares, largura, inter, jaw_x = presets.get(mlc, (dados.get("leaf_pairs") or "CONFIRMAR",
                                                     dados.get("leaf_width_mm") or "CONFIRMAR", "CONFIRMAR", "CONFIRMAR"))
    q = lambda k, padrao="CONFIRMAR": dados.get(k) if dados.get(k) not in (None, "") else padrao   # noqa: E731
    raw = {
        "id": mid, "vendor": q("vendor", "Elekta"), "model": q("model"), "serial": q("serial"),
        "dicom_machine_names": q("dicom_machine_names", ["CONFIRMAR"]),
        "sad_mm": q("sad_mm", 1000.0), "energies_mv": q("energias_mv", ["CONFIRMAR"]), "fff": q("fff", False),
        "techniques": q("techniques", ["3D"]),
        "collimation": {
            "jaw_x": {"present": q("jaw_x", jaw_x), "dicom_types": ["ASYMX", "X"]},
            "jaw_y": {"present": True, "dicom_types": ["ASYMY", "Y"]},
            "mlc": {"dicom_type": "MLCX", "preset": mlc or "CONFIRMAR", "leaf_pairs": pares, "leaf_width_mm": largura,
                    "max_overtravel_mm": q("max_overtravel_mm"), "min_gap_mm": q("min_gap_mm"),
                    "interdigitation": q("interdigitation", inter)}},
        "dicom_exportacao": {"distancia_fonte_mm": q("distancias_fonte_mm", {}), "tolerancia": q("tolerancia", ""),
                             "segmentos": "step_and_shoot"},
        "vmat": q("vmat", {}),
        "rascunho": {"criado": dt.datetime.now().isoformat(timespec="seconds"), "por": "copiloto (IA)",
                     "comissionado": False},
    }
    pend = []

    def varrer(d, pref=""):
        for k, v in d.items():
            if isinstance(v, dict):
                varrer(v, f"{pref}{k}.")
            elif v == "CONFIRMAR" or v == ["CONFIRMAR"]:
                pend.append(f"{pref}{k}")
    varrer(raw)
    raw["rascunho"]["pendencias"] = pend
    pasta = paths.home() / "maquinas_rascunho" / mid
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "maquina.yaml").write_text("# Rascunho criado pelo copiloto — NÃO comissionado. Confira tudo.\n" +
                                        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    for e in raw["energies_mv"] if isinstance(raw["energies_mv"], list) else []:
        if e != "CONFIRMAR":
            (pasta / f"{e}MV").mkdir(exist_ok=True)
    falta_feixe = [it[0] for b in CHECKLIST if b["bloco"].startswith("Dados") for it in b["itens"]]
    return {"pasta": str(pasta), "pendencias": pend, "dados_de_feixe_a_enviar": falta_feixe}


# ------------------------------------------------------------------------------------------------ leitura
def _r(v, n=2):
    return None if v is None else round(float(v), n)


def _estatisticas(dose: np.ndarray, m: np.ndarray, rx: float, alvo: bool) -> dict:
    d = np.sort(dose[m])[::-1]
    if not d.size:
        return {}
    p = lambda v: _r(np.percentile(d, 100 - v))   # noqa: E731
    out = {"Dmean": _r(d.mean()), "Dmax": _r(d[0]), "D2": p(2), "D50": p(50), "D95": p(95), "D98": p(98)}
    if alvo and rx:
        out.update({f"V{k}_pct": _r((d >= k / 100 * rx).mean() * 100, 1) for k in (95, 100, 107)})
    return out


class Ferramentas:
    """Executa as ferramentas do copiloto sobre uma sessão do TPS."""

    def __init__(self, sessao):
        self.s = sessao
        self.acoes: list[str] = []
        self.parecer: dict | None = None
        self.instantaneo = False

    # -- estado para desfazer
    def _guardar(self):
        if self.instantaneo or self.s.plano is None:
            return
        s = self.s
        s.__dict__["_copiloto_desfazer"] = {
            "plano": copy.deepcopy(s.plano), "doses_um": dict(s.doses_um), "dose_fx": s.dose_fx,
            "resultado": s.resultado, "dose_atualizada": s.dose_atualizada,
            "contornos": {n: [c.copy() for c in e.contornos] for n, e in s.estruturas.items()}}
        self.instantaneo = True

    def _contexto(self):
        s = self.s
        pr = s.plano.prescricao
        est = {n: e.mascara for n, e in s.estruturas.items() if e.mascara is not None and e.mascara.any()}
        dose = (s.dose_fx * pr.fracoes) if (s.dose_atualizada and s.dose_fx is not None) else None
        return C.Contexto(est, s.grade, s._corpo(), s.grade.voxel_cc, {}, dose, {})

    def ler_plano(self, _):
        s = self.s
        if s.plano is None:
            return {"erro": "nenhum plano aberto"}
        p = s.plano
        pr = p.prescricao
        campos = []
        for c in p.campos:
            sp = c.__dict__.get("_spec") or {}
            campos.append({"nome": c.nome, "gantry": c.gantry, "arco_fim": c.arco_fim, "sentido": c.sentido or None,
                           "colimador": c.colimador, "mesa": c.mesa, "um": _r(c.mu, 1), "peso": _r(c.peso, 3),
                           "sub_campo_de": c.campo_pai, "segmentos": len(c.segmentos or []),
                           "blindagens": sp.get("blindagens") or [], "alvo": c.alvo_conformado,
                           "margem_mm": c.margem_mm, "descricao": c.descricao or None})
        alvo = s.estruturas.get(pr.estrutura_alvo)
        ests = []
        if s.grade is not None:
            from scipy.ndimage import distance_transform_edt
            g = s.grade
            passo = (abs(g.z[1] - g.z[0]), abs(g.y[1] - g.y[0]), abs(g.x[1] - g.x[0]))
            dist = distance_transform_edt(~alvo.mascara, sampling=passo) if alvo is not None and alvo.mascara.any() \
                else None
            for n, e in s.estruturas.items():
                if e.mascara is None or not e.mascara.any():
                    continue
                it = {"nome": n, "tipo": e.tipo, "volume_cc": _r(e.mascara.sum() * g.voxel_cc, 1)}
                if dist is not None and n != pr.estrutura_alvo:
                    it["dist_ao_alvo_mm"] = _r(dist[e.mascara].min(), 1)
                    it["pct_no_alvo"] = _r((e.mascara & alvo.mascara).sum() / e.mascara.sum() * 100, 1)
                ests.append(it)
        return {"rotulo": p.rotulo, "estado": p.estado, "tecnica": p.tecnica, "perfil": getattr(p, "perfil", "tps"),
                "prescricao": {"dose_total_gy": pr.dose_total_gy, "fracoes": pr.fracoes, "alvo": pr.estrutura_alvo,
                               "normalizacao": pr.metodo, "isodose_pct": pr.isodose_pct},
                "maquina": s.maquina.id if s.maquina else None, "energia": s.maquina.energia if s.maquina else None,
                "isocentro_mm": [_r(v, 1) for v in (s.isocentro or [])], "campos": campos, "estruturas": ests,
                "dose_calculada": bool(s.dose_atualizada), "bolus": p.bolus}

    def ler_dose(self, _):
        s = self.s
        if not s.dose_atualizada or s.dose_fx is None:
            return {"erro": "dose não calculada (use calcular ou otimizar_modulado)"}
        pr = s.plano.prescricao
        tot = s.dose_fx * pr.fracoes
        est = {n: _estatisticas(tot, e.mascara, pr.dose_total_gy, n == pr.estrutura_alvo or e.tipo.upper() in
                                ("PTV", "CTV", "GTV"))
               for n, e in s.estruturas.items() if e.mascara is not None and e.mascara.any()}
        r = s.resultado or {}
        rest = r.get("restricoes") or {}
        res = [{k: x.get(k) for k in ("estrutura", "metrica", "op", "limite", "valor", "ok", "fonte", "tipo")
                if k in x} for x in (rest.get("resultados") or [])][:60]
        conf = [{k: x.get(k) for k in ("campo", "dif_pct", "ok", "feixe", "informativo") if k in x}
                for x in (r.get("conferencia_um") or [])]
        return {"prescricao_gy": pr.dose_total_gy, "fracoes": pr.fracoes, "hi": _r(r.get("hi"), 3),
                "dmax_gy": _r(r.get("dmax_gy")), "dmax_pct": _r(100 * r["dmax_gy"] / pr.dose_total_gy, 1)
                if r.get("dmax_gy") else None, "estruturas": est, "restricoes": res,
                "avisos_restricoes": rest.get("avisos", [])[:10], "conferencia_um": conf,
                "um_total": _r(sum(float(c.mu or 0) for c in s.plano.campos), 1), "avisos": (s.avisos or [])[:15]}

    def dvh(self, e):
        return self._contexto().dvh(str(e.get("estrutura", "")), e.get("doses_gy"), e.get("volumes_pct"))

    def medir(self, e):
        return self._contexto().medir(str(e.get("a", "")), str(e.get("b", "")))

    def ler_campo(self, e):
        c = self.s._campo(str(e.get("nome", "")))
        colim = self.s.maquina.colim
        lim = colim.limites_laminas
        jy = c.jaw_y or (-200, 200)
        pares = [{"par": i + 1, "y_mm": [_r(lim[i], 1), _r(lim[i + 1], 1)], "a_mm": c.mlc_a[i], "b_mm": c.mlc_b[i]}
                 for i in range(colim.n_pares) if lim[i + 1] > jy[0] and lim[i] < jy[1]
                 and c.mlc_b[i] - c.mlc_a[i] > colim.gap_minimo_mm + 0.05]
        return {"nome": c.nome, "gantry": c.gantry, "colimador": c.colimador, "jaw_x": c.jaw_x, "jaw_y": c.jaw_y,
                "pares_abertos": pares, "segmentos": [{"peso": _r(sg["peso"], 3)} for sg in (c.segmentos or [])],
                "blindagem_dividida": bool(c.segmentos_blindagem), "um": _r(c.mu, 1),
                "mlc": {"pares": colim.n_pares, "gap_min_mm": colim.gap_minimo_mm,
                        "interdigitacao": getattr(colim, "interdigitacao", True)}}

    def ler_objetivos(self, _):
        p = self.s.plano
        return {"objetivos": list(p.objetivos_imrt or []), "perfil": getattr(p, "perfil", "tps"), "tecnica": p.tecnica}

    def ler_maquina(self, _):
        m = self.s.maquina
        if m is None:
            return {"erro": "sem máquina"}
        raw = m.raw
        pend = []

        def varrer(d, pref=""):
            for k, v in (d.items() if isinstance(d, dict) else []):
                if isinstance(v, dict):
                    varrer(v, f"{pref}{k}.")
                elif v == "CONFIRMAR":
                    pend.append(f"{pref}{k}")
        varrer(raw)
        return {"id": m.id, "modelo": f"{raw.get('vendor')} {raw.get('model')}", "energia": m.energia,
                "tecnicas": raw.get("techniques"), "mlc": raw["collimation"]["mlc"], "vmat": raw.get("vmat"),
                "imrt": raw.get("imrt"), "comissionado": bool(getattr(m.modelo, "comissionado", False)),
                "confirmar": pend[:30]}

    def ler_protocolo(self, e):
        return C._protocolo(str(e.get("sitio", "")))

    def listar_opcoes(self, _):
        from tps.app.sessao import maquinas_disponiveis
        from tps.planejamento import modelos_plano as MP
        s = self.s
        return {"maquinas": maquinas_disponiveis(),
                "maquina_atual": {"id": s.maquina.id, "energia": s.maquina.energia} if s.maquina else None,
                "modelos_3d": [{"id": m["id"], "nome": m.get("nome"), "sitio": m.get("sitio")} for m in MP.lista()],
                "planos_na_sessao": [p.get("rotulo") for p in (s.resumo().get("planos_sessao") or [])],
                "estruturas": [n for n, e in s.estruturas.items() if e.mascara is not None and e.mascara.any()]}

    # -- ações
    def criar_plano(self, e):
        from tps.app.sessao import maquinas_disponiveis
        s = self.s
        self._guardar()
        if e.get("alvo") not in s.estruturas:
            return {"erro": f"alvo {e.get('alvo')} não existe; estruturas: {list(s.estruturas)[:40]}"}
        if e.get("maquina") or s.maquina is None:
            mid = e.get("maquina") or maquinas_disponiveis()[0]["id"]
            s.definir_maquina(mid, e.get("energia") or "6MV")
        elif e.get("energia") and e["energia"] != s.maquina.energia:
            s.definir_maquina(s.maquina.id, e["energia"])
        s.novo_plano(str(e["rotulo"])[:16], float(e["dose_total_gy"]), int(e["fracoes"]), e["alvo"],
                     e.get("normalizacao") or "media_alvo")
        p = s.plano
        return {"plano": p.rotulo, "maquina": s.maquina.id, "energia": s.maquina.energia,
                "isocentro_mm": [round(float(v), 1) for v in s.isocentro]}

    def criar_campos(self, e):
        s = self.s
        self._guardar()
        if s.plano is None:
            return {"erro": "crie o plano antes (criar_plano)"}
        tec = e.get("tecnica")
        alvo = e.get("alvo") or s.plano.prescricao.estrutura_alvo
        margem = float(e.get("margem_mm") if e.get("margem_mm") is not None else 7.0)
        if tec == "modelo":
            pr = s.plano.prescricao
            rot = s.plano.rotulo
            if not s.plano.campos:                      # plano vazio recém-criado: o modelo o substitui
                s.remover_plano(rot)
            else:
                rot = rot + "_m" if len(rot) < 15 else rot[:14] + "_m"
            r = s.aplicar_modelo(str(e.get("modelo")), rot, pr.dose_total_gy, pr.fracoes, alvo, calcular=False)
            return {"plano": s.plano.rotulo, "campos": [c.nome for c in s.plano.campos], "avisos": (r or {}).get("avisos")}
        if tec == "IMRT":
            s.campos_imrt(int(e.get("n") or 7), alvo=alvo, margem_mm=float(e.get("margem_mm") or 5.0),
                          colimador=float(e.get("colimador") or 0.0))
        elif tec in ("VMAT", "DCA"):
            f = s.campos_vmat if tec == "VMAT" else s.campos_dca
            f(n_arcos=int(e.get("n_arcos") or (2 if tec == "VMAT" else 1)), alvo=alvo,
              margem_mm=float(e.get("margem_mm") or 5.0))
        else:
            gs = [float(g) for g in (e.get("gantries") or [0, 90, 180, 270])]
            for c in [c for c in s.plano.campos if c.campo_pai is None]:
                s.remover_campo(c.nome)
            avisos = []
            for g in gs:
                r = s.salvar_campo({"gantry": g, "colimador": float(e.get("colimador") or 0.0), "alvo": alvo,
                                    "margem": {"x1": margem, "x2": margem, "y1": margem, "y2": margem},
                                    "blindagens": e.get("blindagens") or [],
                                    "otimizar_colimador": bool(e.get("otimizar_colimador"))})
                avisos += r["avisos"]
            return {"campos": [c.nome for c in s.plano.campos], "avisos": avisos[:15]}
        return {"campos": [c.nome for c in s.plano.campos], "tecnica": s.plano.tecnica}

    def definir_perfil(self, e):
        from tps.planejamento import perfil_sistema as PS
        self._guardar()
        self.s.plano._exigir_rascunho()
        self.s.plano.perfil = PS._perfil(e.get("perfil"))
        return {"perfil": self.s.plano.perfil}

    def definir_objetivos(self, e):
        from tps.planejamento import imrt as I
        self._guardar()
        p = self.s.plano
        p._exigir_rascunho()
        ok = []
        for o in e.get("objetivos") or []:
            if o.get("estrutura") not in self.s.estruturas or o.get("tipo") not in I.TIPOS:
                return {"erro": f"objetivo inválido: {o}"}
            ok.append({k: o.get(k) for k in ("estrutura", "tipo", "dose_gy", "volume_pct", "peso", "restrito")
                       if o.get(k) is not None})
        if not any(o["tipo"] == "alvo" for o in ok):
            return {"erro": "inclua pelo menos um objetivo de alvo"}
        p.objetivos_imrt = ok
        return {"objetivos": len(ok)}

    def otimizar_modulado(self, e):
        self._guardar()
        p = self.s.plano
        tec = e.get("tecnica", "IMRT")
        if not p.objetivos_imrt:
            return {"erro": "sem objetivos: use definir_objetivos"}
        corpo = {"objetivos": p.objetivos_imrt, "perfil": getattr(p, "perfil", "tps"),
                 **{k: e[k] for k in ("niveis", "um_min", "area_min_cm2", "peso_nto", "iteracoes") if e.get(k)}}
        self.s.calcular({"vmat" if tec == "VMAT" else "imrt": corpo})
        return {"ok": True, **self._curto()}

    def calcular(self, e):
        self._guardar()
        op = {k: e[k] for k in ("otimizar_pesos", "field_in_field", "compensador", "proteger_oars") if e.get(k)}
        if op.get("compensador") is True:
            op["compensador"] = {"niveis": 6}
        self.s.calcular(op)
        return {"ok": True, **self._curto()}

    def _curto(self):
        d = self.ler_dose({})
        if "erro" in d:
            return d
        pr = self.s.plano.prescricao
        alvo = d["estruturas"].get(pr.estrutura_alvo, {})
        falhas = [f"{r['estrutura']} {r['metrica']}" for r in d["restricoes"] if r.get("ok") is False][:10]
        return {"hi": d["hi"], "dmax_pct": d["dmax_pct"], "alvo": alvo, "restricoes_violadas": falhas,
                "um_total": d["um_total"]}

    def salvar_campo(self, e):
        self._guardar()
        r = self.s.salvar_campo(dict(e.get("spec") or {}))
        return {"campo": r["campo"]["nome"], "avisos": r["avisos"], "segmentos": r["campo"]["n_segmentos"]}

    def remover_campo(self, e):
        self._guardar()
        self.s.remover_campo(str(e.get("nome", "")))
        return {"ok": True}

    def bloquear_orgao(self, e):
        self._guardar()
        c = self.s._campo(str(e.get("campo", "")))
        if e.get("estrutura") not in self.s.estruturas:
            return {"erro": f"estrutura {e.get('estrutura')} não existe"}
        spec = dict(c.__dict__.get("_spec") or {"gantry": c.gantry, "colimador": c.colimador, "mesa": c.mesa,
                                                "alvo": c.alvo_conformado, "margem": c.margem_mm or 7})
        if spec.get("modo") == "retangular" or c.e_arco:
            return {"erro": f"{c.nome} não é um campo conformado: use mover_laminas"}
        spec.setdefault("alvo", c.alvo_conformado or self.s.plano.prescricao.estrutura_alvo)
        bl = [b for b in spec.get("blindagens") or [] if b.get("nome") != e["estrutura"]]
        bl.append({"nome": e["estrutura"], "margem": float(e.get("margem_mm") or 0)})
        spec.setdefault("peso", c.peso / max(1, len(c.segmentos_blindagem or [])))
        spec.update({"nome": c.nome, "nome_antigo": c.nome, "blindagens": bl,
                     "prioridade_alvo": bool(e.get("prioridade_alvo", True))})
        r = self.s.salvar_campo(spec)
        return {"campo": c.nome, "avisos": r["avisos"], "segmentos": r["campo"]["n_segmentos"]}

    def mover_laminas(self, e):
        self._guardar()
        c = self.s._campo(str(e.get("campo", "")))
        a, b = list(c.mlc_a), list(c.mlc_b)
        n = len(a)
        for q in e.get("pares") or []:
            i = int(q.get("par", 0)) - 1
            if not 0 <= i < n:
                return {"erro": f"par {i + 1} fora de 1..{n}"}
            if q.get("a_mm") is not None:
                a[i] = float(q["a_mm"])
            if q.get("b_mm") is not None:
                b[i] = float(q["b_mm"])
        self.s.editar_laminas(c.nome, a, b)
        return {"ok": True, "campo": c.nome}

    def pos_processar_contorno(self, e):
        self._guardar()
        return self.s.pos_processar_contorno(str(e.get("estrutura", "")), float(e.get("suavizar_mm", 2.0) or 0),
                                             float(e.get("fechar_mm", 0) or 0), float(e.get("abrir_mm", 0) or 0),
                                             bool(e.get("preencher", True)), float(e.get("ilhas_cc", 0.5) or 0))

    def checklist_comissionamento(self, _):
        return checklist()

    def criar_rascunho_maquina(self, e):
        return criar_rascunho_maquina(dict(e.get("dados") or {}))

    def dar_parecer(self, e):
        self.parecer = {"aval": e.get("aval"), "resumo": str(e.get("resumo", ""))[:2000],
                        "pontos": [str(x) for x in e.get("pontos") or []][:20],
                        "sugestoes": [str(x) for x in e.get("sugestoes") or []][:20],
                        "objetivos_sugeridos": list(e.get("objetivos_sugeridos") or [])[:40]}
        return {"registrado": True}

    def executar(self, nome: str, entrada: dict) -> dict:
        if getattr(self, "somente_leitura", False) and nome in ACOES:
            return {"erro": "nesta consulta o copiloto só avalia (sem alterar o plano)"}
        f = getattr(self, nome, None)
        if f is None or nome.startswith("_") or nome not in {t["name"] for t in FERRAMENTAS}:
            return {"erro": f"ferramenta desconhecida {nome}"}
        try:
            r = f(entrada or {})
        except Exception as ex:  # noqa: BLE001 - o erro volta para o Claude, que explica ou tenta outra coisa
            return {"erro": str(ex)[:400]}
        if nome in ACOES and "erro" not in r:
            resumo = json.dumps(entrada, ensure_ascii=False, default=str)[:160]
            self.acoes.append(f"{nome} {resumo}")
            try:
                if self.s.plano is not None:
                    self.s._registrar(f"Copiloto (IA): {nome} {resumo}")
                    self.s.plano.historico.append({"evento": "copiloto", "acao": nome, "detalhe": resumo,
                                                   "quando": dt.datetime.now().isoformat(timespec="seconds")})
            except Exception:  # noqa: BLE001
                pass
        return r


def desfazer(sessao) -> dict:
    st = sessao.__dict__.pop("_copiloto_desfazer", None)
    if not st:
        raise ValueError("Nada do copiloto para desfazer")
    sessao.plano = st["plano"]
    sessao.doses_um.clear()
    sessao.doses_um.update(st["doses_um"])
    sessao.dose_fx, sessao.resultado, sessao.dose_atualizada = st["dose_fx"], st["resultado"], st["dose_atualizada"]
    for n, cs in st["contornos"].items():
        e = sessao.estruturas.get(n)
        if e is not None and len(e.contornos) != len(cs) or (e is not None and any(
                a.shape != b.shape or not np.array_equal(a, b) for a, b in zip(e.contornos, cs))):
            e.contornos = cs
            sessao._depois_de_editar(e, registrar=False)
    return {"ok": True}


def _anonimo(texto: str, sessao) -> str:
    return C.anonimizar(texto, getattr(sessao, "paciente", None))


def _sem_raciocinio_antigo(msgs: list) -> None:
    """Tira os blocos de raciocínio (thinking) dos pedidos anteriores. A API só exige esses blocos, intactos, dentro
    do pedido em andamento (entre as chamadas de ferramenta); reenviados em pedidos seguintes — e depois de a conversa
    ser encurtada — a assinatura deles não confere ("Invalid signature in thinking block … different conversation")."""
    for m in msgs:
        if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
            continue
        cont = [b for b in m["content"] if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
        m["content"] = cont or [{"type": "text", "text": "(sem texto)"}]


def conversar(sessao, texto: str, progresso=None, novo: bool = False, somente_leitura: bool = False) -> dict:
    """Um pedido do usuário ao copiloto: o Claude lê, age e responde. A conversa fica na sessão."""
    cfg = C.config()
    if not cfg["chave"]:
        raise RuntimeError("Copiloto de IA não configurado (Ferramentas › Assistente de IA)")
    if novo or "_copiloto_msgs" not in sessao.__dict__:
        sessao.__dict__["_copiloto_msgs"] = []
    msgs = sessao.__dict__["_copiloto_msgs"]
    _sem_raciocinio_antigo(msgs)
    n0 = len(msgs)
    msgs.append({"role": "user", "content": _anonimo(texto, sessao)})
    cli = C._cliente(cfg["chave"])
    fer = Ferramentas(sessao)
    fer.somente_leitura = somente_leitura
    ferramentas = [t for t in FERRAMENTAS if not (somente_leitura and t["name"] in ACOES)]
    t0, uso = time.time(), {"entrada": 0, "saida": 0}
    resposta = ""
    for volta in range(MAX_VOLTAS):
        if progresso:
            progresso(f"Copiloto trabalhando… ({volta + 1})")
        try:
            r = cli.messages.create(model=cfg["modelo"], max_tokens=6000, system=SISTEMA, tools=ferramentas,
                                    messages=msgs)
        except Exception:
            del msgs[n0:]                     # o pedido que falhou não fica pela metade na conversa
            raise
        u = getattr(r, "usage", None)
        if u is not None:
            uso["entrada"] += int(getattr(u, "input_tokens", 0) or 0)
            uso["saida"] += int(getattr(u, "output_tokens", 0) or 0)
        blocos = [C._bloco(b) for b in r.content]
        msgs.append({"role": "assistant", "content": blocos})
        chamadas = [b for b in blocos if b.get("type") == "tool_use"]
        texto_v = "".join(b.get("text", "") for b in blocos if b.get("type") == "text").strip()
        if texto_v:
            resposta = texto_v
        if not chamadas:
            break
        res = []
        for b in chamadas:
            if progresso:
                progresso(f"Copiloto: {b['name'].replace('_', ' ')}…")
            saida = fer.executar(b["name"], b.get("input") or {})
            res.append({"type": "tool_result", "tool_use_id": b["id"],
                        "content": json.dumps(saida, ensure_ascii=False, default=str)[:30000]})
        msgs.append({"role": "user", "content": res})
    else:
        resposta = resposta or "Parei aqui (muitas etapas). Diga se devo continuar."
    del msgs[:-60]                                               # a conversa não cresce sem limite
    while msgs and not (msgs[0]["role"] == "user" and isinstance(msgs[0]["content"], str)):
        msgs.pop(0)                                              # começa sempre num pedido do usuário
    arq = C._registrar({"tipo": "copiloto", "modelo": cfg["modelo"], "quando": dt.datetime.now().isoformat(),
                        "pedido": msgs and texto, "acoes": fer.acoes, "parecer": fer.parecer, "uso": uso,
                        "conversa": msgs[-20:]})
    return {"resposta": resposta, "acoes": fer.acoes, "parecer": fer.parecer, "pode_desfazer": fer.instantaneo
            or "_copiloto_desfazer" in sessao.__dict__, "modelo": cfg["modelo"], "uso_tokens": uso,
            "tempo_s": round(time.time() - t0, 1), "registro": arq and Path(arq).name}
