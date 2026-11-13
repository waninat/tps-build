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
    with tempfile.TemporaryDirectory() as tmp:
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
    from tps.app.servidor import main as servidor
    servidor()
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
