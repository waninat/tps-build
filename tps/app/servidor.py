"""Servidor local da interface do TPS (FastAPI). Abre no navegador em http://127.0.0.1:8770.

    python -m tps.app.servidor [--porta 8770] [--rede]

Por padrão só aceita conexões do próprio computador. Toda a física roda aqui; o navegador só desenha.
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
import threading
import traceback
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from tps import paths
from tps.app.banco import PAPEIS, PODE_APROVAR, PODE_EXPORTAR, Banco, ErroAcesso
from tps.app.sessao import ErroUsuario, Sessao, maquinas_disponiveis, mover_para_lixeira
from tps.version import __version__

ESTATICOS = Path(__file__).parent / "estatico"
app = FastAPI(title="TPS", version=__version__)
SESSOES: dict[str, Sessao] = {}
TRAVA = threading.Lock()                 # um cálculo por vez (numba já usa todos os núcleos)
_BANCO: Banco | None = None


def banco() -> Banco:
    global _BANCO
    if _BANCO is None:                      # um banco por processo, na pasta de dados do TPS
        _BANCO = Banco(paths.home() / "tps.db")
    return _BANCO


def usuario(x_token: str | None = Header(default=None), t: str | None = Query(default=None)) -> dict:
    return banco().usuario_do_token(x_token or t)


def auditar(u: dict, acao: str, s: Sessao | None = None, **det):
    caso = f"{s.paciente.get('id', '')}/{s.id}" if s else None
    banco().auditar(u["login"], acao, caso, det)


@app.exception_handler(ErroUsuario)
async def _erro_usuario(_: Request, e: ErroUsuario):
    return JSONResponse({"erro": str(e)}, status_code=400)


@app.exception_handler(PermissionError)
async def _bloqueado(_: Request, e: PermissionError):
    return JSONResponse({"erro": str(e), "login": isinstance(e, ErroAcesso) and str(e) == "Faça login"},
                        status_code=401 if isinstance(e, ErroAcesso) else 400)


@app.exception_handler(Exception)
async def _erro(req: Request, e: Exception):
    traceback.print_exc()
    txt = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    try:                                         # registro para o suporte (a janela preta pode ser fechada)
        import datetime as _dt
        with open(paths.home() / "erros.log", "a", encoding="utf-8") as f:
            f.write(f"\n===== {_dt.datetime.now():%Y-%m-%d %H:%M:%S}  TPS {__version__}  {req.method} {req.url.path}\n{txt}")
    except Exception:  # noqa: BLE001
        pass
    tb = traceback.extract_tb(e.__traceback__)
    onde = next((f"{Path(q.filename).name}:{q.lineno}" for q in reversed(tb) if "tps" in q.filename), "")
    return JSONResponse({"erro": f"Erro interno: {type(e).__name__}: {e}" + (f" [{onde}]" if onde else "")
                         + " — detalhes em erros.log na pasta de dados do TPS"}, status_code=500)


def _s(sid: str) -> Sessao:
    s = SESSOES.get(sid)
    if s is None:
        raise ErroUsuario("Sessão expirada: abra o caso de novo")
    return s


def _versao_estaticos() -> str:
    """Muda a cada versão e a cada mudança nos arquivos: a janela (WebView2, que guarda cache entre aberturas) nunca
    mistura o HTML novo com o CSS/JS antigos."""
    import hashlib
    h = hashlib.sha1(__version__.encode())
    for n in ("app.js", "estilo.css"):
        f = ESTATICOS / n
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()[:12]


@app.get("/", response_class=HTMLResponse)
def inicio():
    v = _versao_estaticos()
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    html = html.replace('/estatico/estilo.css"', f'/estatico/estilo.css?v={v}"').replace(
        '/estatico/app.js"', f'/estatico/app.js?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.middleware("http")
async def _sem_cache_estatico(request, call_next):
    r = await call_next(request)
    if request.url.path.startswith("/estatico/"):
        r.headers["Cache-Control"] = "no-cache"          # revalida sempre (ETag); evita CSS/JS de versão antiga
    return r


app.mount("/estatico", StaticFiles(directory=ESTATICOS), name="estatico")


@app.get("/api/info")
def info():
    return {"versao": __version__, "maquinas": maquinas_disponiveis(), "home": str(paths.home()),
            "configurar": not banco().tem_usuarios(), "papeis": PAPEIS, "ia": _estado_ia()}


def _estado_ia():
    try:
        from tps.ia.segmentacao import estado
        return estado()
    except Exception as e:  # noqa: BLE001
        return {"disponivel": False, "motivo": str(e), "orgaos": []}


@app.get("/api/objetivos_sitio")
def objetivos_sitio(u: dict = Depends(usuario)):
    from tps.planejamento import assistente
    return assistente.lista()


@app.get("/api/claude")
def claude_estado(u: dict = Depends(usuario)):
    from tps.planejamento import assistente_claude as C
    return C.estado()


@app.post("/api/claude")
def claude_configurar(d: dict = Body(...), u: dict = Depends(usuario)):
    """Chave da API da Anthropic e modelo do assistente (físico/administrador; fica na pasta de dados)."""
    from tps.planejamento import assistente_claude as C
    banco().assinar(u, d.get("senha", ""), {"admin", "fisico"}, "configurar o assistente com Claude")
    try:
        if d.get("apagar_chave"):
            r = C.apagar_chave()
        else:
            r = C.salvar_config(d.get("chave"), d.get("modelo"), d.get("ativo"))
    except ValueError as e:
        raise ErroUsuario(str(e)) from e
    auditar(u, "claude_configurado", None, modelo=r["modelo"], ativo=r["ativo"], chave_apagada=bool(d.get("apagar_chave")))
    return r


@app.post("/api/claude/testar")
def claude_testar(u: dict = Depends(usuario)):
    from tps.planejamento import assistente_claude as C
    try:
        return C.testar()
    except Exception as e:  # noqa: BLE001
        raise ErroUsuario(f"Falhou: {C.mensagem_erro(e)}") from e


@app.get("/api/ia/instalacao")
def ia_instalacao(u: dict = Depends(usuario)):
    from tps.ia import instalador
    return instalador.estado()


@app.post("/api/ia/instalar")
def ia_instalar(d: dict = Body(...), u: dict = Depends(usuario)):
    """Instala o TotalSegmentator em <dados>/ia/venv (em segundo plano; ~2 GB, precisa de internet)."""
    from tps.ia import instalador
    banco().assinar(u, d.get("senha", ""), {"admin", "fisico"}, "instalar o contorno por IA")
    try:
        r = instalador.instalar(bool(d.get("gpu")))
    except RuntimeError as e:
        raise ErroUsuario(str(e)) from e
    auditar(u, "ia_instalacao_iniciada", None, gpu=bool(d.get("gpu")))
    return r


@app.post("/api/ia/apontar")
def ia_apontar(d: dict = Body(...), u: dict = Depends(usuario)):
    from tps.ia import instalador
    banco().assinar(u, d.get("senha", ""), {"admin", "fisico"}, "configurar o contorno por IA")
    try:
        r = instalador.apontar(str(d.get("executavel", "")))
    except RuntimeError as e:
        raise ErroUsuario(str(e)) from e
    auditar(u, "ia_executavel", None, executavel=str(d.get("executavel", "")))
    return r


@app.post("/api/configurar")
def configurar(d: dict = Body(...)):
    """Primeiro uso: cria o primeiro usuário (físico, com direito de gerenciar usuários)."""
    if banco().tem_usuarios():
        raise ErroAcesso("O TPS já tem usuários")
    banco().criar_usuario(d["login"], d["nome"], "fisico", d["senha"])
    token, u = banco().entrar(d["login"], d["senha"])
    return {"token": token, "usuario": u}


@app.post("/api/login")
def login(d: dict = Body(...)):
    token, u = banco().entrar(d.get("login", ""), d.get("senha", ""))
    return {"token": token, "usuario": u}


@app.post("/api/logout")
def logout(x_token: str | None = Header(default=None)):
    banco().sair(x_token or "")
    return {}


@app.get("/api/eu")
def eu(u: dict = Depends(usuario)):
    return u


@app.get("/api/usuarios")
def usuarios(u: dict = Depends(usuario)):
    return banco().usuarios()


@app.post("/api/usuarios")
def criar_usuario(d: dict = Body(...), u: dict = Depends(usuario)):
    banco().assinar(u, d.get("senha_admin", ""), {"admin", "fisico"}, "gerenciar usuários")
    banco().criar_usuario(d["login"], d["nome"], d["papel"], d["senha"], por=u["login"])
    return banco().usuarios()


@app.post("/api/senha")
def trocar_senha(d: dict = Body(...), u: dict = Depends(usuario)):
    banco().trocar_senha(u["login"], d["atual"], d["nova"])
    return {}


@app.get("/api/auditoria")
def auditoria(caso: str | None = None, u: dict = Depends(usuario)):
    return {"registros": banco().auditoria(300, caso), "verificacao": banco().verificar_auditoria()}


@app.get("/api/planos")
def planos(u: dict = Depends(usuario)):
    return banco().planos()


@app.post("/api/caso/ct")
def caso_ct(d: dict = Body(...), u: dict = Depends(usuario)):
    s = Sessao()
    with TRAVA:
        s.carregar_ct(d["pasta"], d.get("curva", "siemens_spirit_oncomed"), float(d.get("voxel_mm", 3.0)))
    SESSOES[s.id] = s
    auditar(u, "caso_aberto", s, pasta=d["pasta"])
    return s.resumo()


@app.post("/api/caso/upload")
async def caso_upload(arquivos: list[UploadFile] = File(...), u: dict = Depends(usuario)):
    pasta = Path(tempfile.mkdtemp(prefix="tps_caso_", dir=paths.home()))
    for f in arquivos:
        destino = pasta / Path(f.filename or "x").name
        with open(destino, "wb") as out:
            shutil.copyfileobj(f.file, out)
    s = Sessao()
    with TRAVA:
        s.carregar_ct(pasta)
    SESSOES[s.id] = s
    auditar(u, "caso_enviado", s, arquivos=len(arquivos))
    return s.resumo()


@app.post("/api/caso/fantoma")
def caso_fantoma(d: dict = Body(...), u: dict = Depends(usuario)):
    s = Sessao()
    s.criar_fantoma(d.get("tipo", "caixa"), float(d.get("largura", 300)), float(d.get("espessura", 200)),
                    float(d.get("comprimento", 300)), d.get("contorno"), float(d.get("voxel_mm", 3.0)),
                    d.get("paciente_id", ""), d.get("paciente_nome", ""))
    SESSOES[s.id] = s
    auditar(u, "fantoma_criado", s, tipo=d.get("tipo"))
    return s.resumo()


@app.post("/api/caso/abrir")
def caso_abrir(d: dict = Body(...), u: dict = Depends(usuario)):
    s = Sessao()
    with TRAVA:
        s.abrir(d["arquivo"])
    SESSOES[s.id] = s
    auditar(u, "plano_aberto", s, arquivo=d["arquivo"])
    return s.resumo()


@app.get("/api/modelos_plano")
def modelos_plano(u: dict = Depends(usuario)):
    from tps.planejamento import modelos_plano as MP
    return MP.lista()


@app.get("/api/{sid}")
def resumo(sid: str, u: dict = Depends(usuario)):
    return _s(sid).resumo()


@app.post("/api/{sid}/maquina")
def maquina(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    s.definir_maquina(d["id"], d.get("energia", "6MV"))
    return s.resumo()


@app.post("/api/{sid}/plano")
def plano(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    s.novo_plano(d.get("rotulo", "PLANO1"), float(d["dose_total"]), int(d["fracoes"]), d.get("alvo", ""),
                 d.get("metodo", "media_alvo"), d.get("ponto"), float(d.get("isodose", 100)),
                 (d.get("cobertura_volume_pct", 95), d.get("cobertura_dose_pct", 100)))
    auditar(u, "plano_criado", s, **{k: d.get(k) for k in ("rotulo", "dose_total", "fracoes", "alvo", "metodo")})
    return s.resumo()


@app.post("/api/{sid}/modelo_plano")
def aplicar_modelo(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.aplicar_modelo(str(d.get("modelo", "")), str(d.get("rotulo", "PLANO1")), d.get("dose_total"),
                             d.get("fracoes"), d.get("alvo") or None, bool(d.get("calcular", True)))
    auditar(u, "plano_de_modelo", s, modelo=d.get("modelo"), rotulo=d.get("rotulo"))
    return r


@app.post("/api/{sid}/isocentro")
def isocentro(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    s.definir_isocentro(d["xyz"])
    return s.resumo()


@app.post("/api/{sid}/campo")
def campo(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.salvar_campo(d)
    auditar(u, "campo_salvo", s, campo=r["campo"]["nome"], gantry=r["campo"]["gantry"])
    return {**s.resumo(), "campo_salvo": r}


@app.post("/api/{sid}/campo/{nome}/laminas")
def laminas(sid: str, nome: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    s.editar_laminas(nome, d["mlc_a"], d["mlc_b"], d.get("jaw_x"), d.get("jaw_y"))
    auditar(u, "laminas_editadas", s, campo=nome)
    return s.resumo()


@app.post("/api/{sid}/campo/{nome}/lamina")
def mover_lamina(sid: str, nome: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        if d.get("jaw"):
            s.mover_jaw(nome, str(d["jaw"]), float(d["pos"]))
        else:
            s.mover_lamina(nome, int(d["par"]), str(d["banco"]), float(d["pos"]))
    auditar(u, "laminas_editadas", s, campo=nome)
    return s.resumo()


@app.post("/api/{sid}/campo/{nome}/desenho")
def desenho_laminas(sid: str, nome: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.desenhar_laminas(nome, d.get("pontos") or [], str(d.get("modo", "abrir")), d.get("encontro", "fora"))
    auditar(u, "laminas_desenhadas", s, campo=nome, modo=d.get("modo"))
    return {**s.resumo(), "desenho_avisos": r.get("avisos", [])}


@app.delete("/api/{sid}/campo/{nome}")
def remover(sid: str, nome: str, u: dict = Depends(usuario)):
    s = _s(sid)
    s.remover_campo(nome)
    auditar(u, "campo_removido", s, campo=nome)
    return s.resumo()


@app.post("/api/{sid}/calcular")
def calcular(sid: str, d: dict = Body(default={}), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.calcular(d)
    auditar(u, "dose_calculada", s, opcoes=d, hi=r.get("resultado", {}).get("hi"))
    return r


@app.post("/api/{sid}/imrt/campos")
def imrt_campos(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.campos_imrt(int(d.get("n", 7)), float(d.get("inicio", 0)), d.get("alvo") or None,
                          float(d.get("margem_mm", 5)), float(d.get("colimador", 0)))
    auditar(u, "imrt_campos", s, n=d.get("n"), inicio=d.get("inicio"))
    return r


@app.post("/api/{sid}/dca/arcos")
def dca_arcos(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    """Arcos conformacionais dinâmicos (DCA) — arco 3D sem modulação."""
    col = d.get("colimadores", "auto")
    if isinstance(col, str) and col not in ("", "auto"):
        col = [float(x) for x in col.replace(";", ",").split(",") if x.strip()]
    with TRAVA:
        return _s(sid).campos_dca(int(d.get("n", 1)), float(d.get("inicio", 181)), float(d.get("fim", 179)),
                                  str(d.get("sentido", "CW")), d.get("alvo") or None, float(d.get("margem_mm", 5)),
                                  col if isinstance(col, list) else "auto", float(d.get("passo_cp", 5)),
                                  d.get("oars") or None, float(d.get("max_bloqueio_pct", 25)), d.get("mesas") or None)


@app.get("/api/{sid}/cq_calculo")
def cq_calculo(sid: str, u: dict = Depends(usuario)):
    with TRAVA:
        return _s(sid).cq_por_calculo()


@app.post("/api/{sid}/vmat/arcos")
def vmat_arcos(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    cols = d.get("colimadores")
    if isinstance(cols, str):
        cols = [float(x) for x in cols.replace(";", ",").split(",") if x.strip()]
    with TRAVA:
        r = s.campos_vmat(int(d.get("n", 1)), float(d.get("inicio", 181)), float(d.get("fim", 179)),
                          d.get("sentido", "CW"), d.get("alvo") or None, float(d.get("margem_mm", 5)), cols)
    auditar(u, "vmat_arcos", s, n=d.get("n"), inicio=d.get("inicio"), fim=d.get("fim"), sentido=d.get("sentido"))
    return r


@app.get("/api/{sid}/imrt/objetivos")
def imrt_objetivos(sid: str, refazer: bool = False, sitio: str = "", u: dict = Depends(usuario)):
    with TRAVA:
        return _s(sid).objetivos_imrt_sugeridos(refazer, sitio or None)


@app.post("/api/{sid}/imrt/assistente")
def imrt_assistente(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    """Lê a descrição do caso + TC/estruturas e propõe os objetivos pelo protocolo do sítio (com a explicação)."""
    s = _s(sid)
    motor = d.get("motor") or "regras"
    if motor not in ("regras", "claude", "auto"):
        raise ErroUsuario("Motor do assistente inválido")
    with TRAVA:
        r = s.assistente_objetivos(str(d.get("texto", ""))[:2000], d.get("sitio") or None, bool(d.get("aplicar")),
                                   motor, d.get("proposta") if d.get("aplicar") else None)
    if r.get("motor") == "claude":
        auditar(u, "assistente_claude", s, modelo=r.get("modelo"), tokens=r.get("uso_tokens"))
    if d.get("aplicar"):
        auditar(u, "assistente_objetivos", s, sitio=r.get("sitio"), texto=str(d.get("texto", ""))[:300])
    return r




@app.get("/api/{sid}/progresso")
def progresso(sid: str, u: dict = Depends(usuario)):
    """Etapa de um cálculo longo em andamento (sem a trava do cálculo)."""
    return {"progresso": getattr(_s(sid), "progresso", None)}


@app.post("/api/{sid}/densidade")
def densidade(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    s.definir_densidade(d["estrutura"], d.get("valor"))
    auditar(u, "densidade_definida", s, estrutura=d["estrutura"], valor=d.get("valor"))
    return s.resumo()


@app.post("/api/{sid}/bolus")
def bolus(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        nome = s.criar_bolus(float(d.get("espessura_mm", 5)), d.get("campos") or [])
    auditar(u, "bolus_criado", s, nome=nome, espessura_mm=d.get("espessura_mm"), campos=d.get("campos"))
    return s.resumo()


@app.delete("/api/{sid}/bolus/{nome}")
def remover_bolus(sid: str, nome: str, u: dict = Depends(usuario)):
    s = _s(sid)
    s.remover_bolus(nome)
    auditar(u, "bolus_removido", s, nome=nome)
    return s.resumo()


@app.post("/api/{sid}/plano/trocar")
def plano_trocar(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        return s.trocar_plano(str(d.get("rotulo", "")))


@app.post("/api/{sid}/plano/copiar")
def plano_copiar(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.copiar_plano(str(d.get("rotulo", "")))
    auditar(u, "plano_copiado", s)
    return r


@app.delete("/api/{sid}/plano/{rotulo}")
def plano_remover(sid: str, rotulo: str, u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.remover_plano(rotulo)
    auditar(u, "plano_removido_da_sessao", s, rotulo=rotulo)
    return r


@app.post("/api/{sid}/plano/apagar")
def plano_apagar(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    """Apaga um plano (rascunho): sai da sessão, o arquivo vai para a lixeira e sai do índice. Pede a senha."""
    s = _s(sid)
    banco().assinar(u, d.get("senha", ""), set(PAPEIS) - {"medico"}, "apagar planos")
    rotulo = str(d.get("rotulo", ""))
    with TRAVA:
        r, chaves = s.apagar_plano(rotulo)
    for c in chaves:
        banco().apagar_plano(c)
    auditar(u, "plano_apagado", s, rotulo=rotulo, arquivos=chaves)
    return r


@app.post("/api/planos/apagar")
def planos_apagar_salvo(d: dict = Body(...), u: dict = Depends(usuario)):
    """Apaga um plano salvo que não está aberto (lista de planos): arquivo para a lixeira, sai do índice."""
    banco().assinar(u, d.get("senha", ""), set(PAPEIS) - {"medico"}, "apagar planos")
    chave = str(d.get("chave", ""))
    reg = next((r for r in banco().planos() if r["chave"] == chave), None)
    if reg is None:
        raise ErroUsuario("Plano não encontrado no índice")
    if reg["estado"] != "RASCUNHO":
        raise ErroUsuario("Plano aprovado não é apagado: abra, desaprove (fica na auditoria) e apague")
    mover_para_lixeira(Path(reg["arquivo"]))
    banco().apagar_plano(chave)
    auditar(u, "plano_apagado", None, chave=chave, rotulo=reg["rotulo"], paciente=reg["paciente_id"])
    return {"ok": True}


@app.post("/api/{sid}/setup")
def setup(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    s.adicionar_setup(d.get("nome", ""), float(d.get("gantry", 0)), float(d.get("x_cm", 20)), float(d.get("y_cm", 20)),
                      float(d.get("colimador", 0)), float(d.get("mesa", 0)))
    auditar(u, "campo_setup", s, **{k: d.get(k) for k in ("nome", "gantry", "x_cm", "y_cm")})
    return s.resumo()


@app.delete("/api/{sid}/setup/{nome}")
def remover_setup(sid: str, nome: str, u: dict = Depends(usuario)):
    s = _s(sid)
    s.remover_setup(nome)
    auditar(u, "campo_setup_removido", s, nome=nome)
    return s.resumo()


@app.post("/api/{sid}/importar_plano")
def importar_plano(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.importar_plano(str(d.get("arquivo", "")))
    auditar(u, "plano_importado", s, arquivo=d.get("arquivo"))
    return r


@app.post("/api/{sid}/contorno/{acao}")
def contorno(sid: str, acao: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        if acao == "nova":
            r = s.nova_estrutura(d.get("nome", ""), d.get("tipo", "ORGAN"), d.get("cor"))
        elif acao == "editar":
            r = s.editar_contorno(d.get("nome", ""), int(d.get("idx", -1)), d.get("ferramenta", ""), d.get("pontos"),
                                  float(d.get("raio_mm", 5.0)), bool(d.get("apagar", False)), d.get("origem"))
        elif acao == "interpolar":
            r = s.interpolar_contorno(d.get("nome", ""))
        elif acao == "corpo":
            r = s.corpo_automatico(d.get("nome", "BODY") or "BODY", float(d.get("limiar", -350)))
        elif acao == "desfazer":
            r = s.desfazer_contorno()
        elif acao == "operar":
            r = s.operar_contorno(d.get("nome", ""), d.get("operacao", ""), d.get("outra", ""),
                                  float(d.get("margem_mm", 0) or 0))
        elif acao == "orgaos":
            r = s.orgaos_automaticos(list(d.get("orgaos") or []))
        elif acao == "ia":
            z = d.get("reto_zmax_mm")
            r = s.segmentar_ia(list(d.get("orgaos") or []), float(z) if z not in (None, "") else None)
        else:
            raise ErroUsuario(f"Ação de contorno desconhecida: {acao}")
    if acao != "editar":
        auditar(u, f"contorno_{acao}", s, **{k: v for k, v in r.items() if k != "pontos"})
    return {**s.resumo(), "contorno": r}


@app.post("/api/{sid}/estrutura")
def estrutura(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.criar_estrutura(d.get("nome", ""), d.get("operacao", ""), d.get("a", ""), d.get("b"),
                              d.get("margens"), bool(d.get("contrair", False)), float(d.get("interno_mm", 0) or 0),
                              float(d.get("externo_mm", 0) or 0), d.get("nivel_gy"), d.get("cor"))
    auditar(u, "estrutura_criada", s, **{k: v for k, v in d.items() if k != "cor"}, volume_cc=r["volume_cc"])
    return s.resumo()


@app.delete("/api/{sid}/estrutura/{nome}")
def remover_estrutura(sid: str, nome: str, u: dict = Depends(usuario)):
    s = _s(sid)
    s.remover_estrutura(nome)
    auditar(u, "estrutura_removida", s, nome=nome)
    return s.resumo()


@app.post("/api/{sid}/ponto")
def ponto(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    s.adicionar_ponto(d.get("nome", ""), d["xyz"])
    return s.resumo()


@app.delete("/api/{sid}/ponto/{nome}")
def remover_ponto(sid: str, nome: str, u: dict = Depends(usuario)):
    s = _s(sid)
    s.remover_ponto(nome)
    return s.resumo()


@app.get("/api/{sid}/dose")
def dose_ponto(sid: str, x: float, y: float, z: float, u: dict = Depends(usuario)):
    return _s(sid).dose_em((x, y, z))


@app.get("/api/{sid}/relatorio_medico.pdf")
def relatorio_medico(sid: str, u: dict = Depends(usuario)):
    from tps.app.relatorio_pdf import relatorio_medico_pdf
    s = _s(sid)
    with TRAVA:
        try:
            pdf = relatorio_medico_pdf(s, u)
        except ValueError as e:
            raise ErroUsuario(str(e)) from e
    auditar(u, "relatorio_medico_pdf", s)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="medico_{s.plano.rotulo}.pdf"'})


@app.get("/api/{sid}/relatorio.pdf")
def relatorio_pdf(sid: str, u: dict = Depends(usuario)):
    from tps.app.relatorio_pdf import relatorio_pdf as gerar
    s = _s(sid)
    with TRAVA:
        dados = gerar(s, u)
    auditar(u, "relatorio_pdf", s)
    nome = f"Relatorio_{s.plano.rotulo if s.plano else 'plano'}.pdf"
    return Response(dados, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{nome}"'})


@app.get("/api/{sid}/checkfilm.pdf")
def checkfilm_pdf(sid: str, u: dict = Depends(usuario)):
    from tps.app.checkfilm import folha_pdf
    s = _s(sid)
    if not s.plano or not s.plano.campos:
        raise ErroUsuario("Plano sem campos")
    with TRAVA:
        dados = folha_pdf(s)
    auditar(u, "checkfilm_pdf", s)
    return Response(dados, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="CheckFilm_{s.plano.rotulo}.pdf"'})


@app.post("/api/{sid}/restricao")
def restricao_nova(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    na_clinica = bool(d.get("na_clinica"))
    if na_clinica and u["papel"] not in PODE_EXPORTAR:
        raise ErroUsuario("Só o físico pode alterar o protocolo da clínica")
    r = s.adicionar_restricao(d, na_clinica)
    auditar(u, "restricao_clinica" if na_clinica else "restricao_plano", s,
            **{k: d.get(k) for k in ("estrutura", "metrica", "op", "valor", "tipo")})
    return r


@app.delete("/api/{sid}/restricao/{indice}")
def restricao_remover(sid: str, indice: int, u: dict = Depends(usuario)):
    s = _s(sid)
    r = s.remover_restricao(indice)
    auditar(u, "restricao_removida", s, indice=indice)
    return r


@app.get("/api/{sid}/restricoes")
def restricoes(sid: str, u: dict = Depends(usuario)):
    return _s(sid).restricoes_json()


@app.get("/api/{sid}/dvh")
def dvh(sid: str, u: dict = Depends(usuario)):
    return _s(sid).dvh_json()


@app.get("/api/{sid}/corte/{vista}/{idx}.png")
def corte(sid: str, vista: str, idx: int, janela: float = 400, nivel: float = 40, u: dict = Depends(usuario)):
    return Response(_s(sid).imagem_corte(vista, idx, janela, nivel), media_type="image/png",
                    headers={"Cache-Control": "max-age=600"})


@app.get("/api/{sid}/dose_corte/{vista}/{idx}.png")
def dose_corte(sid: str, vista: str, idx: int, minimo: float = 20.0, maximo: float = 0.0, fonte: str = "plano",
               u: dict = Depends(usuario)):
    if vista not in ("axial", "coronal", "sagital"):
        raise ErroUsuario("vista inválida")
    fonte = fonte if fonte in ("plano", "soma", "dif") else "plano"
    return Response(_s(sid).imagem_dose(vista, idx, minimo, maximo, fonte=fonte), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/{sid}/sobreposicao/{vista}/{idx}")
def sobreposicao(sid: str, vista: str, idx: int, isodoses: bool = True, niveis: str = "", ref: str = "rx",
                 unidade: str = "pct", fonte: str = "plano", niveis_gy: str = "", u: dict = Depends(usuario)):
    try:
        lista = [float(x) for x in niveis.replace(";", ",").split(",") if x.strip()] or None
        extras = [float(x) for x in niveis_gy.replace(";", ",").split(",") if x.strip()]
    except ValueError as e:
        raise ErroUsuario("Níveis de isodose inválidos") from e
    return _s(sid).sobreposicao(vista, idx, None, isodoses, lista, "max" if ref == "max" else "rx",
                                "gy" if unidade == "gy" else "pct",
                                fonte if fonte in ("plano", "soma", "dif") else "plano", extras)


@app.get("/api/{sid}/checklist")
def checklist(sid: str, u: dict = Depends(usuario)):
    from tps.app.checklist import checklist as chk
    with TRAVA:
        return chk(_s(sid))


@app.post("/api/{sid}/soma_planos")
def soma_planos(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        r = s.soma_planos(list(d.get("rotulos") or []), str(d.get("modo", "fisica")),
                          float(d.get("ab_alvo", 10)), float(d.get("ab_oar", 3)), d.get("ab_estruturas") or {})
    auditar(u, "soma_planos", s, rotulos=r["rotulos"], modo=r["modo"])
    return r


@app.post("/api/{sid}/comparar_planos")
def comparar_planos(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    with TRAVA:
        return s.comparar_planos(str(d.get("a", "")), str(d.get("b", "")))


@app.get("/api/{sid}/bev/{nome}")
def bev(sid: str, nome: str, u: dict = Depends(usuario)):
    with TRAVA:
        return _s(sid).bev(nome)


@app.get("/api/{sid}/drr/{nome}.png")
def drr(sid: str, nome: str, u: dict = Depends(usuario)):
    with TRAVA:
        return Response(_s(sid).drr(nome), media_type="image/png")


@app.post("/api/{sid}/aprovar")
def aprovar(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    a = banco().assinar(u, d.get("senha", ""), PODE_APROVAR, "aprovar planos")
    s.aprovar(a["login"], PAPEIS[a["papel"]], d.get("justificativa", ""), a["papel"] in PODE_EXPORTAR)
    auditar(u, "plano_aprovado", s, hash=s.plano.aprovacao["hash"],
            justificativa_um=(s.plano.aprovacao.get("conferencia_um_justificativa") or {}).get("justificativa"))
    return s.resumo()


@app.post("/api/{sid}/desaprovar")
def desaprovar(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    a = banco().assinar(u, d.get("senha", ""), PODE_APROVAR, "desaprovar planos")
    s.desaprovar(a["login"], d.get("motivo", ""))
    auditar(u, "plano_desaprovado", s, motivo=d.get("motivo", ""))
    return s.resumo()


@app.post("/api/{sid}/exportar")
def exportar(sid: str, d: dict = Body(...), u: dict = Depends(usuario)):
    s = _s(sid)
    banco().assinar(u, d.get("senha", ""), PODE_EXPORTAR, "exportar planos")
    destino = d.get("pasta") or str(paths.home() / "exportados" / s.id)
    r = s.exportar(destino, d.get("modo_segmentos"), d.get("incluir_imagens"), bool(d.get("rtstruct", True)))
    auditar(u, "plano_exportado", s, arquivos=r["arquivos"], hash=s.plano.aprovacao["hash"])
    if d.get("enviar"):
        e = d["enviar"]
        r["envio"] = s.enviar_dicom(r["arquivos"], e["host"], int(e["porta"]), e["ae_destino"], e.get("ae_local", "TPS"))
        auditar(u, "plano_enviado", s, destino=f"{e['ae_destino']}@{e['host']}:{e['porta']}")
    return r


@app.post("/api/{sid}/qa_paciente")
def qa_paciente(sid: str, d: dict = Body(default={}), u: dict = Depends(usuario)):
    s = _s(sid)
    destino = d.get("pasta") or str(paths.home() / "exportados" / s.id / "QA")
    pr = d.get("profundidade_iso_mm")
    with TRAVA:
        r = s.gerar_qa(destino, str(d.get("fantoma", "cubo30")), str(d.get("modo", "real")),
                       float(pr) if pr not in (None, "") else None)
    auditar(u, "qa_paciente", s, modo=r["modo"], arquivos=r["arquivos"])
    return r


@app.get("/api/{sid}/arquivo")
def baixar(sid: str, caminho: str, u: dict = Depends(usuario)):
    p = Path(caminho).resolve()
    base = paths.home().resolve()
    if base not in p.parents or not p.is_file():
        raise ErroUsuario("Arquivo fora da pasta do TPS")
    return FileResponse(p, filename=p.name)


@app.post("/api/{sid}/salvar")
def salvar(sid: str, u: dict = Depends(usuario)):
    s = _s(sid)
    f = s.salvar()
    p = s.plano
    banco().registrar_plano(f.stem, s.paciente.get("id", ""), s.paciente.get("nome", ""), p.rotulo, p.estado,
                            s.maquina.id, str(f), u["login"], (p.aprovacao or {}).get("hash"))
    auditar(u, "plano_salvo", s, arquivo=str(f))
    return {"arquivo": str(f)}


@app.get("/api/{sid}/relatorio", response_class=HTMLResponse)
def relatorio(sid: str, u: dict = Depends(usuario)):
    return _s(sid).relatorio_html()


def argumentos(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8770)
    ap.add_argument("--rede", action="store_true", help="aceita conexões de outros computadores da rede")
    ap.add_argument("--home", help="pasta de dados do TPS (padrão: ~/TPS ou %%ProgramData%%\\TPS)")
    ap.add_argument("--sem-navegador", action="store_true", help="só o servidor (sem janela nem navegador)")
    ap.add_argument("--navegador", action="store_true", help="abre no navegador em vez da janela própria")
    ap.add_argument("--teste-janela", action="store_true", help="abre a janela, confere a página e fecha")
    a, _ = ap.parse_known_args(argv)
    return a


def escolher_porta(porta: int) -> int:
    import socket
    for _ in range(10):                      # porta ocupada (outro TPS aberto?): tenta a seguinte
        with socket.socket() as so:
            if so.connect_ex(("127.0.0.1", porta)) != 0:
                return porta
        print(f"A porta {porta} já está em uso; tentando {porta + 1}.", flush=True)
        porta += 1
    return porta


def servidor_em_paralelo(host: str, porta: int):
    """Servidor uvicorn numa linha paralela (para a janela própria ficar na linha principal)."""
    import uvicorn
    srv = uvicorn.Server(uvicorn.Config(app, host=host, port=porta, log_level="warning"))
    srv.install_signal_handlers = lambda: None
    threading.Thread(target=srv.run, daemon=True, name="servidor").start()
    return srv


def main(argv=None):
    a = argumentos(argv)
    if a.home:
        paths.set_home(a.home)
    paths.ensure_home()
    import socket

    import uvicorn
    host = "0.0.0.0" if a.rede else "127.0.0.1"
    porta = escolher_porta(a.porta)
    url = f"http://127.0.0.1:{porta}"

    def abrir_quando_pronto():
        """Só abre o navegador quando o servidor já aceita conexões (a 1ª execução pode demorar)."""
        import time
        t0 = time.time()
        while time.time() - t0 < 300:
            with socket.socket() as so:
                so.settimeout(1)
                if so.connect_ex(("127.0.0.1", porta)) == 0:
                    print(f"Pronto: {url}  (feche esta janela para encerrar o TPS)", flush=True)
                    if not a.sem_navegador:
                        import webbrowser
                        webbrowser.open(url)
                    return
            time.sleep(0.5)
        print(f"O servidor não respondeu em 5 minutos. Tente abrir {url} no navegador.", flush=True)

    print(f"TPS {__version__}: iniciando em {url} … (a primeira execução pode levar 1–2 minutos)", flush=True)
    threading.Thread(target=abrir_quando_pronto, daemon=True).start()
    uvicorn.run(app, host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
