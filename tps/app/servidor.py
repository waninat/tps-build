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
from tps.app.sessao import ErroUsuario, Sessao, maquinas_disponiveis
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
async def _erro(_: Request, e: Exception):
    traceback.print_exc()
    return JSONResponse({"erro": f"Erro interno: {type(e).__name__}: {e}"}, status_code=500)


def _s(sid: str) -> Sessao:
    s = SESSOES.get(sid)
    if s is None:
        raise ErroUsuario("Sessão expirada: abra o caso de novo")
    return s


@app.get("/", response_class=HTMLResponse)
def inicio():
    return (ESTATICOS / "index.html").read_text(encoding="utf-8")


app.mount("/estatico", StaticFiles(directory=ESTATICOS), name="estatico")


@app.get("/api/info")
def info():
    return {"versao": __version__, "maquinas": maquinas_disponiveis(), "home": str(paths.home()),
            "configurar": not banco().tem_usuarios(), "papeis": PAPEIS}


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
                 d.get("metodo", "media_alvo"), d.get("ponto"), float(d.get("isodose", 100)))
    auditar(u, "plano_criado", s, **{k: d.get(k) for k in ("rotulo", "dose_total", "fracoes", "alvo", "metodo")})
    return s.resumo()


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
        else:
            raise ErroUsuario(f"Ação de contorno desconhecida: {acao}")
    if acao != "editar":
        auditar(u, f"contorno_{acao}", s, **{k: v for k, v in r.items() if k != "pontos"})
    return {"resultado": r, **s.resumo()}


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
def dose_corte(sid: str, vista: str, idx: int, minimo: float = 20.0, maximo: float = 0.0, u: dict = Depends(usuario)):
    if vista not in ("axial", "coronal", "sagital"):
        raise ErroUsuario("vista inválida")
    return Response(_s(sid).imagem_dose(vista, idx, minimo, maximo), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/{sid}/sobreposicao/{vista}/{idx}")
def sobreposicao(sid: str, vista: str, idx: int, isodoses: bool = True, u: dict = Depends(usuario)):
    return _s(sid).sobreposicao(vista, idx, None, isodoses)


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
    s.aprovar(a["login"], PAPEIS[a["papel"]])
    auditar(u, "plano_aprovado", s, hash=s.plano.aprovacao["hash"])
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--porta", type=int, default=8770)
    ap.add_argument("--rede", action="store_true", help="aceita conexões de outros computadores da rede")
    ap.add_argument("--home", help="pasta de dados do TPS (padrão: ~/TPS ou %%ProgramData%%\\TPS)")
    ap.add_argument("--sem-navegador", action="store_true")
    a = ap.parse_args()
    if a.home:
        paths.set_home(a.home)
    paths.ensure_home()
    import socket

    import uvicorn
    host = "0.0.0.0" if a.rede else "127.0.0.1"
    porta = a.porta
    for _ in range(10):                      # porta ocupada (outro TPS aberto?): tenta a seguinte
        with socket.socket() as so:
            if so.connect_ex(("127.0.0.1", porta)) != 0:
                break
        print(f"A porta {porta} já está em uso; tentando {porta + 1}.", flush=True)
        porta += 1
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
