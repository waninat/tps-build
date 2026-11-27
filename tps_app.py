"""Ponto de entrada do programa instalado (TPS.exe).

    TPS.exe                       inicia o servidor local e abre o navegador
    TPS.exe --porta 8780 --rede   mesmas opções de `python -m tps.app.servidor`
    TPS.exe --casos-teste PASTA   grava os casos de teste (DICOM) na pasta
    TPS.exe --autoteste           confere o motor de cálculo e a API (usado no GitHub Actions)
"""
from __future__ import annotations

import os
import sys


def _cache_numba():
    r"""O numba guarda o código de máquina compilado ao lado do módulo; em C:\Program Files um usuário comum não
    pode escrever, e no Windows o tempfile fica tentando para sempre (o TPS travava ao iniciar). Usa uma pasta do
    usuário. Precisa vir antes de qualquer import do numba."""
    if os.environ.get("NUMBA_CACHE_DIR"):
        return
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    pasta = os.path.join(base, "TPS", "numba")
    try:
        os.makedirs(pasta, exist_ok=True)
        os.environ["NUMBA_CACHE_DIR"] = pasta
    except OSError:
        pass


_cache_numba()


def autoteste() -> int:
    import tempfile
    from pathlib import Path

    from tps import paths
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:  # Windows: tps.db ainda aberto
        paths.set_home(Path(tmp) / "home")
        paths.ensure_home()
        from fastapi.testclient import TestClient

        from tps.app.servidor import app
        from tps.casos_teste import main as casos
        sys.argv = ["casos", str(Path(tmp) / "casos")]
        casos()
        c = TestClient(app)
        tok = c.post("/api/configurar", json={"login": "auto", "nome": "Autoteste", "senha": "autoteste-1"}).json()["token"]
        c.headers["X-Token"] = tok
        s = c.post("/api/caso/ct", json={"pasta": str(Path(tmp) / "casos" / "cilindro")}).json()
        sid = s["id"]
        c.post(f"/api/{sid}/maquina", json={"id": "radioserra_precise"})
        c.post(f"/api/{sid}/plano", json={"rotulo": "AUTO", "dose_total": 50, "fracoes": 25, "alvo": "PTV_fantoma"})
        for g in (0, 90, 180, 270):
            c.post(f"/api/{sid}/campo", json={"gantry": g})
        r = c.post(f"/api/{sid}/calcular", json={}).json()
        conf = r["resultado"]["conferencia_um"]
        ok = all(k["ok"] for k in conf) and r["resultado"]["estatisticas"]["PTV_fantoma"]["D95"] > 47
        # Monte Carlo compilado (núcleo numba paralelo): um campo 10×10 em água, poucas histórias
        try:
            import numpy as np

            from tps.app.sessao import Maquina
            from tps.engine.phantom import square_field, water_box
            from tps.mc import calculo as MC
            mq = Maquina.carregar("radioserra_precise", "6MV")
            g = water_box(200.0, 200.0, 5.0)
            seg = square_field(10.0, 10.0, 1000.0, 1000.0, 1.0, np.asarray(mq.colim.limites_laminas))
            r = MC.transportar(g, [seg], mq.modelo, incerteza_pct=10, historias_max=300_000, historias_min=300_000)
            bom = bool(np.isfinite(r.dose).all() and r.dose.max() > 0)
            print("MC", "ok" if bom else "FALHOU", f"{r.historias} histórias")
            ok = ok and bom
        except Exception as e:  # noqa: BLE001
            print("AUTOTESTE MC FALHOU:", e)
            ok = False
        # assistente com Claude: o cliente da API monta sem rede (pega falta de metadados no programa compilado)
        try:
            from tps.planejamento import assistente_claude as C
            C._cliente("sk-ant-autoteste")
            from importlib import metadata
            print("anthropic", metadata.version("anthropic"), "httpx2", metadata.version("httpx2"))
        except Exception as e:  # noqa: BLE001
            print("AUTOTESTE claude FALHOU:", e)
            ok = False
        print("AUTOTESTE", "OK" if ok else "FALHOU", [round(k["dif_pct"], 2) for k in conf])
        paths.set_home(None)
        return 0 if ok else 1


def _registro_inicio():
    """Mostra cada etapa da inicialização e, se travar, grava a pilha a cada 30 s em <dados>/inicio.log."""
    import faulthandler
    import time
    from tps import paths
    from tps.version import __version__
    t0 = time.time()
    try:
        paths.ensure_home()
        log = open(paths.home() / "inicio.log", "w", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"Aviso: não consegui criar o registro em {paths.home()}: {e}", flush=True)
        log = None

    def etapa(msg):
        linha = f"[{time.time() - t0:6.1f} s] {msg}"
        print(linha, flush=True)
        if log:
            log.write(linha + "\n")
            log.flush()
    etapa(f"TPS {__version__} — iniciando (dados em {paths.home()}; cache do cálculo em "
          f"{os.environ.get('NUMBA_CACHE_DIR', 'padrão')})")
    if log:
        faulthandler.dump_traceback_later(30, repeat=True, file=log)
    return etapa


def _carregar(etapa):
    etapa("carregando numpy/scipy…")
    import numpy  # noqa: F401
    import scipy.ndimage  # noqa: F401
    etapa("carregando numba (núcleo de cálculo)…")
    import numba  # noqa: F401
    etapa("carregando matplotlib…")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot  # noqa: F401
    etapa("carregando pydicom/servidor…")
    import fastapi  # noqa: F401
    import pydicom  # noqa: F401
    etapa("carregando o TPS…")


def _janela_propria():
    """Abre o TPS na janela própria (WebView2). Devolve o código de saída, ou None para voltar ao navegador."""
    import argparse
    try:
        from tps.app import janela
        if not janela.disponivel():
            return None
    except Exception:  # noqa: BLE001
        return None
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--porta", type=int, default=8770)
    ap.add_argument("--rede", action="store_true")
    ap.add_argument("--home")
    ap.add_argument("--teste-janela", action="store_true")
    a, _ = ap.parse_known_args()
    from tps import paths
    if a.home:
        paths.set_home(a.home)
    etapa_log = _registro_inicio()

    def preparar(etapa_tela):
        def etapa(msg):
            etapa_log(msg)
            etapa_tela(msg)
        _carregar(etapa)
        from tps.app import servidor
        etapa("iniciando o servidor…")
        porta = servidor.escolher_porta(a.porta)
        return ("0.0.0.0" if a.rede else "127.0.0.1"), porta, f"http://127.0.0.1:{porta}"

    def iniciar(host, porta):
        from tps.app import servidor
        return servidor.servidor_em_paralelo(host, porta)

    try:
        return janela.rodar(preparar, iniciar, teste=a.teste_janela)
    except Exception as e:  # noqa: BLE001
        print(f"Janela própria indisponível ({e}); abrindo no navegador.", flush=True)
        if a.teste_janela:
            import traceback
            traceback.print_exc()
            return 2
        return None


def _console_se_preciso(saida_em_arquivo: bool = False):
    """O TPS.exe não tem console (abre na janela própria). Sem janela, cria um console para o usuário ver o servidor
    e poder fechá-lo; com janela, manda a saída para <dados>/tps_saida.log (sys.stdout é None num programa sem
    console e o uvicorn tentaria escrever nele)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    if saida_em_arquivo:
        try:
            from tps import paths
            paths.ensure_home()
            saida = paths.home() / "tps_saida.log"
            if saida.exists():                         # guarda a saída da vez anterior (se o TPS caiu, está lá)
                try:
                    os.replace(saida, paths.home() / "tps_saida.anterior.log")
                except OSError:
                    pass
            f = open(saida, "w", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or f
            sys.stderr = sys.stderr or f
            return
        except Exception:  # noqa: BLE001
            pass
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.AllocConsole():
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
            sys.stdin = open("CONIN$", "r", encoding="utf-8")
            ctypes.windll.kernel32.SetConsoleTitleW("TPS (feche esta janela para encerrar)")
            return
    nulo = open(os.devnull, "w")
    sys.stdout = sys.stdout or nulo
    sys.stderr = sys.stderr or nulo


def main() -> int:
    if "--autoteste" in sys.argv:
        return autoteste()
    if "--casos-teste" in sys.argv:
        i = sys.argv.index("--casos-teste")
        from tps.casos_teste import main as casos
        sys.argv = ["casos"] + sys.argv[i + 1:i + 2]
        casos()
        return 0
    if "--versao" in sys.argv:
        from tps.version import __version__
        print(__version__)
        return 0
    try:
        from tps import paths
        from tps.diagnostico import ativar_registro_de_falhas
        paths.ensure_home()
        ativar_registro_de_falhas(paths.home())
    except Exception:  # noqa: BLE001
        pass
    if not any(f in sys.argv for f in ("--navegador", "--sem-navegador")):
        _console_se_preciso(saida_em_arquivo=True)
        r = _janela_propria()
        if r is not None:
            return r
        if "--teste-janela" in sys.argv:
            print("janela própria indisponível (pywebview/WebView2)", flush=True)
            return 2
        if getattr(sys.stdout, "name", "").endswith("tps_saida.log"):   # sem janela: volta a ter console
            sys.stdout = sys.stderr = None
    _console_se_preciso()
    try:
        etapa = _registro_inicio()
        _carregar(etapa)
        from tps.app.servidor import main as servidor
        etapa("iniciando o servidor…")
        servidor()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001
        # sem isto a janela fecha na hora e o erro some: mostra, grava em log e espera o usuário
        import traceback
        txt = traceback.format_exc()
        print(txt, flush=True)
        try:
            from tps import paths
            (paths.home() / "erro_inicializacao.log").write_text(txt, encoding="utf-8")
            print(f"(gravado em {paths.home() / 'erro_inicializacao.log'})")
        except Exception:  # noqa: BLE001
            pass
        if getattr(sys, "frozen", False) or "__compiled__" in globals():
            input("O TPS não conseguiu iniciar. Envie esta mensagem ao suporte. Tecle Enter para fechar.")
        return 1
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
