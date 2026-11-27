"""Instalação do contorno por IA (TotalSegmentator) de dentro do TPS: roda em segundo plano e guarda o registro.

Windows: chama o instalar_ia.ps1 que acompanha o programa (cria <pasta de dados>/ia/venv com o Python 3.11–3.14 (recomendado 3.12) do
python.org). Outros sistemas (desenvolvimento): cria o mesmo venv com o Python que estiver no PATH. Também dá para
apontar um TotalSegmentator já instalado (config.yaml → executavel).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml

from tps import paths

_estado: dict = {"rodando": False, "inicio": None, "fim": None, "codigo": None, "gpu": False}
_trava = threading.Lock()


def _log() -> Path:
    return paths.home() / "ia" / "instalacao.log"


def script_windows() -> Path | None:
    cands = [Path(sys.executable).parent / "ia" / "instalar_ia.ps1",
             paths.PROGRAM / "ia" / "instalar_ia.ps1",
             paths.PROGRAM.parent / "ia" / "instalar_ia.ps1",
             paths.PROGRAM / "empacotar" / "ia" / "instalar_ia.ps1"]
    return next((c for c in cands if c.exists()), None)


def _comando(gpu: bool) -> list[str]:
    destino = paths.home() / "ia"
    if sys.platform == "win32":
        sc = script_windows()
        if sc is None:
            raise RuntimeError("instalar_ia.ps1 não encontrado na pasta do programa (reinstale o TPS)")
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(sc),
               "-Destino", str(destino)]
        return cmd + (["-GPU"] if gpu else [])
    py = shutil.which("python3.12") or shutil.which("python3.11") or shutil.which("python3.13") or \
        (sys.executable if not getattr(sys, "frozen", False) else None)
    if py is None:
        raise RuntimeError("Python 3.11–3.14 (recomendado 3.12) não encontrado")
    venv = destino / "venv"
    pip = venv / "bin" / "pip"
    idx = "https://download.pytorch.org/whl/" + ("cu124" if gpu else "cpu")
    passos = [f'"{py}" -m venv "{venv}"', f'"{pip}" install --upgrade pip',
              f'"{pip}" install torch --index-url {idx}', f'"{pip}" install TotalSegmentator rt_utils',
              f'"{venv}/bin/totalseg_download_weights" -t total_fast']
    return ["/bin/sh", "-c", " && ".join(passos)]


def estado() -> dict:
    from tps.ia.segmentacao import estado as est_seg
    log = _log()
    linhas = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:] if log.exists() else []
    return {**_estado, "plataforma": sys.platform, "script": str(script_windows() or ""),
            "destino": str(paths.home() / "ia"), "log": linhas, "segmentacao": est_seg()}


def instalar(gpu: bool = False) -> dict:
    with _trava:
        if _estado["rodando"]:
            raise RuntimeError("A instalação já está em andamento")
        cmd = _comando(gpu)
        (paths.home() / "ia").mkdir(parents=True, exist_ok=True)
        _estado.update(rodando=True, inicio=time.strftime("%Y-%m-%d %H:%M:%S"), fim=None, codigo=None, gpu=gpu)

    def rodar():
        try:
            with open(_log(), "w", encoding="utf-8") as f:
                f.write(f"$ {' '.join(cmd)}\n")
                f.flush()
                kw = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}
                pr = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT,
                                      env=os.environ.copy(), **kw)
                cod = pr.wait()
        except Exception as e:  # noqa: BLE001
            with open(_log(), "a", encoding="utf-8") as f:
                f.write(f"\nERRO: {e}\n")
            cod = -1
        _estado.update(rodando=False, fim=time.strftime("%Y-%m-%d %H:%M:%S"), codigo=cod)

    threading.Thread(target=rodar, daemon=True).start()
    return estado()


def apontar(executavel: str) -> dict:
    """Usa um TotalSegmentator já instalado (caminho do executável)."""
    p = Path(executavel.strip().strip('"'))
    if not p.exists():
        raise RuntimeError(f"Arquivo não encontrado: {p}")
    cfg = paths.home() / "ia" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    dados = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}) if cfg.exists() else {}
    dados["executavel"] = str(p)
    cfg.write_text(yaml.safe_dump(dados, allow_unicode=True), encoding="utf-8")
    return estado()
