"""Ponto de entrada do programa instalado (TPS.exe).

    TPS.exe                       inicia o servidor local e abre o navegador
    TPS.exe --porta 8780 --rede   mesmas opções de `python -m tps.app.servidor`
    TPS.exe --casos-teste PASTA   grava os casos de teste (DICOM) na pasta
    TPS.exe --autoteste           confere o motor de cálculo e a API (usado no GitHub Actions)
"""
from __future__ import annotations

import sys


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
    etapa(f"TPS {__version__} — iniciando (dados em {paths.home()})")
    if log:
        faulthandler.dump_traceback_later(30, repeat=True, file=log)
    return etapa


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
        etapa = _registro_inicio()
        etapa("carregando numpy/scipy…")
        import numpy  # noqa: F401
        import scipy.ndimage  # noqa: F401
        etapa("carregando numba (núcleo de cálculo)…")
        import numba  # noqa: F401
        etapa("carregando matplotlib…")
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot  # noqa: F401
        etapa("carregando pydicom/servidor web…")
        import fastapi  # noqa: F401
        import pydicom  # noqa: F401
        etapa("carregando o TPS…")
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
