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

from fastapi import Body, FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from tps import paths
from tps.app.sessao import ErroUsuario, Sessao, maquinas_disponiveis
from tps.version import __version__

ESTATICOS = Path(__file__).parent / "estatico"
app = FastAPI(title="TPS", version=__version__)
SESSOES: dict[str, Sessao] = {}
TRAVA = threading.Lock()                 # um cálculo por vez (numba já usa todos os núcleos)


@app.exception_handler(ErroUsuario)
async def _erro_usuario(_: Request, e: ErroUsuario):
    return JSONResponse({"erro": str(e)}, status_code=400)


@app.exception_handler(PermissionError)
async def _bloqueado(_: Request, e: PermissionError):
    return JSONResponse({"erro": str(e)}, status_code=400)


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
            "planos_salvos": sorted(str(p) for p in (paths.home() / "planos").glob("*.json"))
            if (paths.home() / "planos").exists() else []}


@app.post("/api/caso/ct")
def caso_ct(d: dict = Body(...)):
    s = Sessao()
    with TRAVA:
        s.carregar_ct(d["pasta"], d.get("curva", "siemens_spirit_oncomed"), float(d.get("voxel_mm", 3.0)))
    SESSOES[s.id] = s
    return s.resumo()


@app.post("/api/caso/upload")
async def caso_upload(arquivos: list[UploadFile] = File(...)):
    pasta = Path(tempfile.mkdtemp(prefix="tps_caso_", dir=paths.home()))
    for f in arquivos:
        destino = pasta / Path(f.filename or "x").name
        with open(destino, "wb") as out:
            shutil.copyfileobj(f.file, out)
    s = Sessao()
    with TRAVA:
        s.carregar_ct(pasta)
    SESSOES[s.id] = s
    return s.resumo()


@app.post("/api/caso/fantoma")
def caso_fantoma(d: dict = Body(...)):
    s = Sessao()
    s.criar_fantoma(d.get("tipo", "caixa"), float(d.get("largura", 300)), float(d.get("espessura", 200)),
                    float(d.get("comprimento", 300)), d.get("contorno"), float(d.get("voxel_mm", 3.0)),
                    d.get("paciente_id", ""), d.get("paciente_nome", ""))
    SESSOES[s.id] = s
    return s.resumo()


@app.post("/api/caso/abrir")
def caso_abrir(d: dict = Body(...)):
    s = Sessao()
    with TRAVA:
        s.abrir(d["arquivo"])
    SESSOES[s.id] = s
    return s.resumo()


@app.get("/api/{sid}")
def resumo(sid: str):
    return _s(sid).resumo()


@app.post("/api/{sid}/maquina")
def maquina(sid: str, d: dict = Body(...)):
    s = _s(sid)
    s.definir_maquina(d["id"], d.get("energia", "6MV"))
    return s.resumo()


@app.post("/api/{sid}/plano")
def plano(sid: str, d: dict = Body(...)):
    s = _s(sid)
    s.novo_plano(d.get("rotulo", "PLANO1"), float(d["dose_total"]), int(d["fracoes"]), d.get("alvo", ""),
                 d.get("metodo", "media_alvo"), d.get("ponto"), float(d.get("isodose", 100)))
    return s.resumo()


@app.post("/api/{sid}/isocentro")
def isocentro(sid: str, d: dict = Body(...)):
    s = _s(sid)
    s.definir_isocentro(d["xyz"])
    return s.resumo()


@app.post("/api/{sid}/campo")
def campo(sid: str, d: dict = Body(...)):
    s = _s(sid)
    with TRAVA:
        r = s.salvar_campo(d)
    return {**s.resumo(), "campo_salvo": r}


@app.post("/api/{sid}/campo/{nome}/laminas")
def laminas(sid: str, nome: str, d: dict = Body(...)):
    s = _s(sid)
    s.editar_laminas(nome, d["mlc_a"], d["mlc_b"], d.get("jaw_x"), d.get("jaw_y"))
    return s.resumo()


@app.delete("/api/{sid}/campo/{nome}")
def remover(sid: str, nome: str):
    s = _s(sid)
    s.remover_campo(nome)
    return s.resumo()


@app.post("/api/{sid}/calcular")
def calcular(sid: str, d: dict = Body(default={})):
    s = _s(sid)
    with TRAVA:
        return s.calcular(d)


@app.get("/api/{sid}/dvh")
def dvh(sid: str):
    return _s(sid).dvh_json()


@app.get("/api/{sid}/corte/{vista}/{idx}.png")
def corte(sid: str, vista: str, idx: int, janela: float = 400, nivel: float = 40):
    return Response(_s(sid).imagem_corte(vista, idx, janela, nivel), media_type="image/png",
                    headers={"Cache-Control": "max-age=600"})


@app.get("/api/{sid}/sobreposicao/{vista}/{idx}")
def sobreposicao(sid: str, vista: str, idx: int, isodoses: bool = True):
    return _s(sid).sobreposicao(vista, idx, None, isodoses)


@app.get("/api/{sid}/bev/{nome}")
def bev(sid: str, nome: str):
    with TRAVA:
        return _s(sid).bev(nome)


@app.get("/api/{sid}/drr/{nome}.png")
def drr(sid: str, nome: str):
    with TRAVA:
        return Response(_s(sid).drr(nome), media_type="image/png")


@app.post("/api/{sid}/aprovar")
def aprovar(sid: str, d: dict = Body(...)):
    s = _s(sid)
    s.aprovar(d.get("usuario", ""), d.get("papel", "Físico Médico"))
    return s.resumo()


@app.post("/api/{sid}/desaprovar")
def desaprovar(sid: str, d: dict = Body(...)):
    s = _s(sid)
    s.desaprovar(d.get("usuario", ""), d.get("motivo", ""))
    return s.resumo()


@app.post("/api/{sid}/exportar")
def exportar(sid: str, d: dict = Body(...)):
    s = _s(sid)
    destino = d.get("pasta") or str(paths.home() / "exportados" / s.id)
    r = s.exportar(destino, d.get("modo_segmentos", "campos_separados"))
    if d.get("enviar"):
        e = d["enviar"]
        r["envio"] = s.enviar_dicom(r["arquivos"], e["host"], int(e["porta"]), e["ae_destino"], e.get("ae_local", "TPS"))
    return r


@app.get("/api/{sid}/arquivo")
def baixar(sid: str, caminho: str):
    p = Path(caminho).resolve()
    base = paths.home().resolve()
    if base not in p.parents or not p.is_file():
        raise ErroUsuario("Arquivo fora da pasta do TPS")
    return FileResponse(p, filename=p.name)


@app.post("/api/{sid}/salvar")
def salvar(sid: str):
    return {"arquivo": str(_s(sid).salvar())}


@app.get("/api/{sid}/relatorio", response_class=HTMLResponse)
def relatorio(sid: str):
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
    import uvicorn
    host = "0.0.0.0" if a.rede else "127.0.0.1"
    if not a.sem_navegador:
        import webbrowser
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{a.porta}")).start()
    print(f"TPS {__version__} em http://127.0.0.1:{a.porta}  (Ctrl+C para fechar)")
    uvicorn.run(app, host=host, port=a.porta, log_level="warning")


if __name__ == "__main__":
    main()
