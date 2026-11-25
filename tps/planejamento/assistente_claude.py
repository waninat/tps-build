"""Assistente de objetivos com o Claude (API da Anthropic). SRS-PLN-096.

O que sai da clínica: só um RESUMO ANÔNIMO calculado pelo TPS — nomes das estruturas, volumes, distâncias ao alvo,
sobreposições, lado do alvo, distância à pele, prescrição, estatísticas de dose (se houver) — e a descrição digitada
(com o nome e o ID do paciente apagados). Nunca a TC, as imagens, as máscaras, o nome ou o ID do paciente.

O Claude raciocina sobre o resumo, pode pedir medidas ao TPS (ferramentas: protocolo do sítio, distância/
sobreposição entre duas estruturas, DVH) e devolve a proposta pela ferramenta `propor_objetivos`. A proposta passa
pelas regras (estruturas existem, tipos e números válidos, órgão nunca acima do alvo) antes de chegar à tela, e o
planejador revisa e aplica. Cada consulta fica registrada em <dados>/ia/claude/ (o que foi enviado e o que voltou).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

import numpy as np
import yaml

from tps import paths
from tps.planejamento import assistente as A
from tps.planejamento.restricoes import nome_tg263

MODELOS = {"claude-opus-5-5": "Claude Opus 5.5 — melhor raciocínio (padrão)",
           "claude-sonnet-5-5": "Claude Sonnet 5.5 — mais rápido e mais barato"}
MODELO_PADRAO = "claude-opus-5-5"
MAX_VOLTAS = 8
TIPOS = ("alvo", "min", "max", "media", "dvh", "serial", "paralelo")

SISTEMA = """Você é um físico médico experiente ajudando a montar os objetivos de otimização inversa (IMRT/VMAT) num
sistema de planejamento para aceleradores Elekta (Precise com MLCi e Synergy com Agility). Você NÃO vê a TC: recebe
um resumo anônimo calculado pelo TPS (estruturas, volumes, distâncias, sobreposições, prescrição, dose se houver) e a
descrição do caso escrita pelo planejador. Use as ferramentas para pedir medidas ou o protocolo do sítio quando
precisar, e termine SEMPRE chamando `propor_objetivos`.

Tipos de objetivo do TPS (doses em Gy, dose TOTAL do plano):
- alvo: dose uniforme no alvo (penaliza abaixo de 95% e acima de 107%). Um por alvo; vários alvos com doses
  diferentes = boost integrado (o de dose maior manda na sobreposição).
- min: dose mínima no volume. max: dose máxima (vale para todo o volume). media: dose média máxima.
- dvh: no máximo volume_pct % do volume acima de dose_gy.
- serial: EUD com expoente k (em volume_pct; medula 12–20, tronco 12–16, reto 8–12) abaixo de dose_gy.
- paralelo: no máximo volume_pct % acima de dose_gy (sigmoide).
- peso 0,1–10 (alvos 3–10; órgãos 1–3). restrito=true: o otimizador TEM de cumprir (use só para órgãos seriados
  críticos ou limites inegociáveis; demais ficam ponderados).
- Órgãos: o objetivo vale só para a parte FORA do alvo (o TPS já exclui a sobreposição).

Regras:
- Use somente nomes de estruturas que existem (ou as listadas em "a_criar").
- Não invente alvos nem doses de prescrição: as doses dos alvos vêm da prescrição, dos nomes ou da descrição.
- Objetivos de órgão nunca acima da dose do alvo. Para otimizar, ponha os limites clínicos um pouco abaixo
  (máximo ≈ 95%, média e volumes ≈ 90% do limite), como folga.
- Siga o protocolo do sítio quando existir (ferramenta ler_protocolo) e ajuste ao caso: hipofracionamento
  (converta os limites com bom senso e avise), órgão muito sobreposto ao PTV (afrouxe e explique), lado, pós-op.
- Aponte em `avisos` o que precisa da atenção do planejador (órgão faltando, dose ambígua, alvo perto da pele →
  flash/bolus, conflito entre a descrição e a prescrição).
- Escreva a explicação em português, curta, um item por decisão.
"""

FERRAMENTAS = [
    {"name": "ler_protocolo",
     "description": "Limites de dose do protocolo de um sítio (arquivo da clínica ou de fábrica): órgãos TG-263, "
                    "métricas e valores, fracionamento de referência e fonte.",
     "input_schema": {"type": "object", "properties": {"sitio": {"type": "string", "description": "id do sítio"}},
                      "required": ["sitio"]}},
    {"name": "medir",
     "description": "Distância mínima (mm) entre duas estruturas, volume de A dentro de B (cm³ e % de A) e a "
                    "posição relativa dos centros (mm; +x = esquerda do paciente, +y = posterior, +z = cranial).",
     "input_schema": {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                      "required": ["a", "b"]}},
    {"name": "dvh",
     "description": "Só se a dose do plano estiver calculada: volume (%) acima de cada dose e dose recebida por "
                    "cada volume (%) numa estrutura.",
     "input_schema": {"type": "object", "properties": {
         "estrutura": {"type": "string"},
         "doses_gy": {"type": "array", "items": {"type": "number"}},
         "volumes_pct": {"type": "array", "items": {"type": "number"}}}, "required": ["estrutura"]}},
    {"name": "propor_objetivos",
     "description": "Resposta final: os objetivos propostos, a explicação e os avisos.",
     "input_schema": {"type": "object", "properties": {
         "sitio": {"type": "string", "description": "id do protocolo usado, ou vazio"},
         "objetivos": {"type": "array", "items": {"type": "object", "properties": {
             "estrutura": {"type": "string"}, "tipo": {"type": "string", "enum": list(TIPOS)},
             "dose_gy": {"type": "number"}, "volume_pct": {"type": ["number", "null"]},
             "peso": {"type": "number"}, "restrito": {"type": "boolean"},
             "motivo": {"type": "string", "description": "de onde vem (ex.: 'RTOG 0126 V70Gy ≤ 25%')"}},
             "required": ["estrutura", "tipo", "dose_gy"]}},
         "explicacao": {"type": "array", "items": {"type": "string"}},
         "avisos": {"type": "array", "items": {"type": "string"}},
         "arcos": {"type": ["object", "null"], "properties": {
             "n": {"type": "integer"}, "inicio": {"type": "number"}, "fim": {"type": "number"},
             "sentido": {"type": "string", "enum": ["CW", "CC"]}},
             "description": "só se recomendar arcos diferentes do arco cheio (Elekta: gantry −185 a +185; "
                            "arco cheio = 181→179 CW)"}},
         "required": ["objetivos", "explicacao"]}},
]


# ---------------------------------------------------------------------------------------------------- configuração
def _arq_config():
    return paths.home() / "config" / "claude.yaml"


def config() -> dict:
    d = {}
    f = _arq_config()
    if f.exists():
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            d = {}
    chave = os.environ.get("ANTHROPIC_API_KEY") or d.get("chave") or ""
    return {"chave": chave, "modelo": d.get("modelo") or MODELO_PADRAO, "ativo": bool(d.get("ativo", True)),
            "da_variavel": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def estado() -> dict:
    c = config()
    return {"configurado": bool(c["chave"]), "ativo": c["ativo"] and bool(c["chave"]), "modelo": c["modelo"],
            "modelos": MODELOS, "chave_final": c["chave"][-4:] if c["chave"] else "",
            "da_variavel": c["da_variavel"]}


def salvar_config(chave: str | None, modelo: str | None, ativo: bool | None) -> dict:
    f = _arq_config()
    f.parent.mkdir(parents=True, exist_ok=True)
    d = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.exists() else {}
    if chave is not None and chave.strip():
        d["chave"] = chave.strip()
    if modelo:
        if modelo not in MODELOS:
            raise ValueError("Modelo desconhecido")
        d["modelo"] = modelo
    if ativo is not None:
        d["ativo"] = bool(ativo)
    f.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass
    return estado()


def apagar_chave() -> dict:
    f = _arq_config()
    if f.exists():
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        d.pop("chave", None)
        f.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return estado()


def mensagem_erro(e: Exception) -> str:
    """Erro da API em português (sem o corpo técnico)."""
    t = str(e)
    for cod, msg in (("401", "chave da API inválida"), ("403", "chave sem permissão para este modelo"),
                     ("404", "modelo não encontrado para esta chave"), ("429", "limite de uso da conta atingido; tente daqui a pouco"),
                     ("529", "serviço da Anthropic sobrecarregado; tente daqui a pouco"),
                     ("credit", "conta sem créditos (console.anthropic.com › Billing)")):
        if cod in t:
            return msg
    if "Connection" in type(e).__name__ or "connect" in t.lower() or "timed out" in t.lower():
        return "sem conexão com api.anthropic.com (internet, proxy ou firewall da clínica)"
    return t[:200]


def _cliente(chave: str):
    """Cliente da API (os testes trocam esta função)."""
    import anthropic
    return anthropic.Anthropic(api_key=chave, timeout=180.0, max_retries=2)


def testar() -> dict:
    c = config()
    if not c["chave"]:
        raise RuntimeError("Informe a chave da API")
    t0 = time.time()
    r = _cliente(c["chave"]).messages.create(model=c["modelo"], max_tokens=20,
                                              messages=[{"role": "user", "content": "Responda só: ok"}])
    txt = "".join(getattr(b, "text", "") for b in r.content)
    return {"ok": True, "modelo": c["modelo"], "resposta": txt.strip()[:40], "tempo_s": round(time.time() - t0, 1)}


# ---------------------------------------------------------------------------------------------------- resumo anônimo
def anonimizar(texto: str, paciente: dict | None) -> str:
    t = texto or ""
    for v in (paciente or {}).values():
        for parte in re.split(r"[\s^,]+", str(v or "")):
            if len(parte) >= 3:
                t = re.sub(re.escape(parte), "[paciente]", t, flags=re.IGNORECASE)
    t = re.sub(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b", "[cpf]", t)          # CPF
    return t[:2000]


class Contexto:
    """O que o TPS sabe do caso, para o resumo e as ferramentas (fica no servidor; só as respostas saem)."""

    def __init__(self, estruturas: dict, grade, corpo, voxel_cc: float, prescricao: dict, dose_total=None,
                 analise_regras: dict | None = None):
        self.est = {n: m for n, m in estruturas.items() if m is not None and m.any()}
        self.grade, self.corpo, self.voxel_cc = grade, corpo, float(voxel_cc)
        self.rx = prescricao
        self.dose = dose_total
        self.regras = analise_regras or {}
        self.passo = (abs(grade.z[1] - grade.z[0]), abs(grade.y[1] - grade.y[0]), abs(grade.x[1] - grade.x[0]))
        self._dist = {}

    def _edt(self, nome):
        if nome not in self._dist:
            from scipy.ndimage import distance_transform_edt
            self._dist[nome] = distance_transform_edt(~self.est[nome], sampling=self.passo)
        return self._dist[nome]

    def _centro(self, m):
        k, j, i = np.nonzero(m)
        return np.array([np.mean(np.asarray(self.grade.x)[i]), np.mean(np.asarray(self.grade.y)[j]),
                         np.mean(np.asarray(self.grade.z)[k])])

    def medir(self, a: str, b: str) -> dict:
        for n in (a, b):
            if n not in self.est:
                return {"erro": f"estrutura {n} não existe"}
        ma, mb = self.est[a], self.est[b]
        dentro = float((ma & mb).sum())
        d = float(self._edt(b)[ma].min())
        return {"distancia_min_mm": round(d, 1), "a_dentro_de_b_cc": round(dentro * self.voxel_cc, 1),
                "a_dentro_de_b_pct": round(100 * dentro / ma.sum(), 1),
                "centro_b_menos_a_mm": [round(float(v), 1) for v in self._centro(mb) - self._centro(ma)]}

    def dvh(self, nome: str, doses_gy=None, volumes_pct=None) -> dict:
        if self.dose is None:
            return {"erro": "dose do plano não calculada"}
        if nome not in self.est:
            return {"erro": f"estrutura {nome} não existe"}
        d = np.sort(self.dose[self.est[nome]])[::-1]
        out = {"Dmean": round(float(d.mean()), 2), "Dmax": round(float(d[0]), 2),
               "volume_cc": round(d.size * self.voxel_cc, 1)}
        for g in (doses_gy or [])[:20]:
            out[f"V{g:g}Gy_pct"] = round(float((d >= g).mean() * 100), 1)
        for v in (volumes_pct or [])[:20]:
            out[f"D{v:g}%_Gy"] = round(float(np.percentile(d, 100 - v)), 2)
        return out

    def resumo(self, texto_anon: str) -> dict:
        r = self.regras
        alvos = [a["estrutura"] for a in r.get("alvos", [])]
        uniao = np.zeros_like(self.corpo, dtype=bool)
        for a in alvos:
            uniao |= self.est[a]
        dist_alvo = None
        if uniao.any():
            from scipy.ndimage import distance_transform_edt
            dist_alvo = distance_transform_edt(~uniao, sampling=self.passo)
        z = np.asarray(self.grade.z)
        ests = []
        for n, m in self.est.items():
            tg = nome_tg263(n)
            e = {"nome": n, "tg263": tg, "volume_cc": round(float(m.sum() * self.voxel_cc), 1),
                 "alvo": bool(A.PADRAO_ALVO.match(n)), "dose_no_nome_gy": A.dose_do_nome(n) if A.PADRAO_ALVO.match(n) else None,
                 "extensao_z_mm": [round(float(z[np.nonzero(m.any(axis=(1, 2)))[0]].min()), 1),
                                   round(float(z[np.nonzero(m.any(axis=(1, 2)))[0]].max()), 1)]}
            if dist_alvo is not None and n not in alvos and tg != "External":
                e["dist_min_ao_alvo_mm"] = round(float(dist_alvo[m].min()), 1)
                e["pct_dentro_do_alvo"] = round(float((m & uniao).sum() / m.sum() * 100), 1)
            if self.dose is not None:
                d = self.dose[m]
                e["dose_atual"] = {"Dmean": round(float(d.mean()), 2), "Dmax": round(float(d.max()), 2)}
            ests.append(e)
        pele = None
        if uniao.any() and self.corpo.any():
            from scipy.ndimage import distance_transform_edt
            pele = round(float(distance_transform_edt(self.corpo, sampling=self.passo)[uniao].min()), 1)
        return {"descricao_do_caso": texto_anon or "(sem descrição)",
                "prescricao": self.rx, "dose_calculada": self.dose is not None,
                "alvos_identificados": r.get("alvos", []), "lado_do_alvo": (r.get("parametros") or {}).get("lado"),
                "alvo_dist_min_pele_mm": pele, "estruturas": ests,
                "a_criar": r.get("criar", []), "orgaos_faltando": [f["tg"] for f in r.get("faltando", [])],
                "leitura_por_regras": {"sitio": r.get("sitio"), "explicacao": r.get("explicacao", []),
                                       "avisos": r.get("avisos", [])},
                "protocolos_disponiveis": [{"id": s["id"], "nome": s["nome"]} for s in A.lista()],
                "maquina": "Elekta (MLCi: lâminas 1 cm, sem interdigitação; Agility: 0,5 cm)"}


# ---------------------------------------------------------------------------------------------------- conversa
def _protocolo(sitio: str) -> dict:
    s = A.carregar().get(sitio)
    if not s:
        return {"erro": f"protocolo {sitio} não existe", "disponiveis": list(A.carregar())}
    return {k: s.get(k) for k in ("id", "nome", "fonte", "dose_fracao_ref", "orgaos", "essenciais", "lateral")}


def _executar(ctx: Contexto, nome: str, entrada: dict) -> dict:
    try:
        if nome == "ler_protocolo":
            return _protocolo(str(entrada.get("sitio", "")))
        if nome == "medir":
            return ctx.medir(str(entrada.get("a", "")), str(entrada.get("b", "")))
        if nome == "dvh":
            return ctx.dvh(str(entrada.get("estrutura", "")), entrada.get("doses_gy"), entrada.get("volumes_pct"))
    except Exception as e:  # noqa: BLE001
        return {"erro": str(e)}
    return {"erro": f"ferramenta desconhecida {nome}"}


def validar(prop: dict, ctx: Contexto) -> tuple[list[dict], list[str]]:
    """As regras conferem a proposta: estruturas existem, tipos e números válidos, órgão não acima do alvo."""
    existentes = set(ctx.est) | {c["nome"] for c in ctx.regras.get("criar", [])}
    alvos_d = [float(o.get("dose_gy") or 0) for o in prop.get("objetivos", []) if o.get("tipo") == "alvo"]
    dmax_alvo = max(alvos_d) if alvos_d else float(ctx.rx.get("dose_total_gy") or 0)
    ok, avisos = [], []
    for o in (prop.get("objetivos") or [])[:80]:
        n, t = str(o.get("estrutura", "")), str(o.get("tipo", ""))
        if n not in existentes:
            avisos.append(f"Descartado: {n} não existe")
            continue
        if t not in TIPOS:
            avisos.append(f"Descartado: tipo {t} inválido ({n})")
            continue
        try:
            d = float(o.get("dose_gy"))
        except (TypeError, ValueError):
            avisos.append(f"Descartado: {n} sem dose")
            continue
        if not 0 < d <= 120:
            avisos.append(f"Descartado: {n} com dose {d} Gy")
            continue
        if t != "alvo" and dmax_alvo and d > dmax_alvo * 1.10 and t != "min":
            avisos.append(f"Descartado: {n} {t} {d} Gy acima da dose do alvo")
            continue
        v = o.get("volume_pct")
        if t in ("dvh", "paralelo", "serial"):
            try:
                v = float(v)
            except (TypeError, ValueError):
                avisos.append(f"Descartado: {n} {t} sem volume/expoente")
                continue
            if not (0 <= v <= 100 if t != "serial" else 1 <= v <= 40):
                avisos.append(f"Descartado: {n} {t} com {v}")
                continue
        else:
            v = None
        peso = float(o.get("peso") or (5.0 if t == "alvo" else 1.0))
        ok.append({"estrutura": n, "tipo": t, "dose_gy": round(d, 2), "volume_pct": v,
                   "peso": float(min(max(peso, 0.1), 10.0)), "restrito": bool(o.get("restrito")) and t != "alvo",
                   "origem": "Claude: " + str(o.get("motivo") or "")[:120]})
    if not any(o["tipo"] == "alvo" for o in ok):
        avisos.append("A proposta não tem objetivo de alvo: confira")
    return ok, avisos


def _registrar(registro: dict):
    try:
        d = paths.home() / "ia" / "claude"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.json"
        f.write_text(json.dumps(registro, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return str(f)
    except OSError:
        return None


def _bloco(b) -> dict:
    """Bloco de conteúdo do SDK → dict (para reenviar e registrar)."""
    if isinstance(b, dict):
        return b
    if hasattr(b, "model_dump"):
        return b.model_dump(exclude_none=True)
    return {"type": getattr(b, "type", "text"), "text": getattr(b, "text", "")}


def propor(ctx: Contexto, texto_anon: str, progresso=None) -> dict:
    c = config()
    if not c["chave"]:
        raise RuntimeError("Assistente com Claude não configurado (Ferramentas › Assistente com Claude)")
    cli = _cliente(c["chave"])
    resumo = ctx.resumo(texto_anon)
    msgs = [{"role": "user", "content": "Resumo do caso calculado pelo TPS (JSON):\n" +
             json.dumps(resumo, ensure_ascii=False, default=str) +
             "\n\nProponha os objetivos de otimização e termine com propor_objetivos."}]
    t0, proposta, uso = time.time(), None, {"entrada": 0, "saida": 0}
    for volta in range(MAX_VOLTAS):
        forcar = volta == MAX_VOLTAS - 1
        if progresso:
            progresso(f"Claude analisando o caso… ({volta + 1})")
        r = cli.messages.create(model=c["modelo"], max_tokens=8000, system=SISTEMA, tools=FERRAMENTAS,
                                messages=msgs, **({"tool_choice": {"type": "tool", "name": "propor_objetivos"}}
                                                  if forcar else {}))
        u = getattr(r, "usage", None)
        if u is not None:
            uso["entrada"] += int(getattr(u, "input_tokens", 0) or 0)
            uso["saida"] += int(getattr(u, "output_tokens", 0) or 0)
        blocos = [_bloco(b) for b in r.content]
        msgs.append({"role": "assistant", "content": blocos})
        chamadas = [b for b in blocos if b.get("type") == "tool_use"]
        final = next((b for b in chamadas if b.get("name") == "propor_objetivos"), None)
        if final is not None:
            proposta = final.get("input") or {}
            break
        if not chamadas:
            msgs.append({"role": "user", "content": "Termine chamando propor_objetivos."})
            continue
        msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": b["id"],
             "content": json.dumps(_executar(ctx, b["name"], b.get("input") or {}), ensure_ascii=False, default=str)}
            for b in chamadas]})
    if proposta is None:
        raise RuntimeError("O Claude não devolveu a proposta")
    objetivos, descartes = validar(proposta, ctx)
    arquivo = _registrar({"modelo": c["modelo"], "quando": dt.datetime.now().isoformat(), "enviado": msgs,
                          "proposta": proposta, "validados": objetivos, "descartes": descartes, "uso": uso})
    return {"objetivos": objetivos, "explicacao": [str(x) for x in proposta.get("explicacao") or []],
            "avisos": [str(x) for x in proposta.get("avisos") or []] + descartes,
            "sitio": proposta.get("sitio") or None, "arcos": proposta.get("arcos"), "modelo": c["modelo"],
            "tempo_s": round(time.time() - t0, 1), "uso_tokens": uso, "registro": arquivo}
