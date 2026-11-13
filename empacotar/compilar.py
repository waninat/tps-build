"""Compila o TPS num programa instalável (código de máquina, sem os fontes Python) — mesmo método do QualiRT.

    python empacotar/compilar.py [--jobs 4]   → empacotar/saida/TPS/  (pasta do programa, com TPS.exe)

No Windows, o instalador é gerado em seguida pelo Inno Setup (empacotar/tps.iss); no GitHub isso roda sozinho
(.github/workflows/instalador-windows.yml).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
AQUI = RAIZ / "empacotar"
SAIDA = AQUI / "saida"
WINDOWS = sys.platform.startswith("win")
NUMBA = ["tps.engine.ccc", "tps.compare.gamma"]


def versao() -> str:
    return re.search(r'__version__ = "(.+)"', (RAIZ / "tps" / "version.py").read_text(encoding="utf-8")).group(1)


def distribuicoes() -> set[str]:
    from importlib import metadata
    nomes = set()
    for req in [RAIZ / "requirements.txt", AQUI / "requirements-compilacao.txt"]:
        for ln in req.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z0-9_.\-]+)", ln)
            if m and not ln.strip().startswith("#"):
                nomes.add(m.group(1))
    nomes |= {"llvmlite", "numpy", "pillow", "pdfminer.six", "packaging", "starlette", "pydantic", "anyio"}
    nomes -= {"nuitka", "ordered-set", "zstandard", "pytest"}
    ok = set()
    for n in nomes:
        try:
            ok.add(metadata.distribution(n).metadata["Name"])
        except metadata.PackageNotFoundError:
            pass
    return ok


def copia_console(origem: Path, destino: Path) -> None:
    """Gêmeo de console do executável (mesmo binário, subsistema CUI) para a linha de comando."""
    import struct
    b = bytearray(origem.read_bytes())
    pe = struct.unpack_from("<I", b, 0x3C)[0]
    if b[pe:pe + 4] != b"PE\0\0":
        raise SystemExit("executável sem cabeçalho PE")
    struct.pack_into("<H", b, pe + 24 + 68, 3)
    destino.write_bytes(bytes(b))


def nuitka(jobs: int | None) -> Path:
    v = versao()
    cmd = [
        sys.executable, "-m", "nuitka", "--standalone", "--assume-yes-for-downloads",
        f"--output-dir={SAIDA}", "--output-filename=TPS", "--remove-output",
        f"--user-plugin={AQUI / 'plugin_tps.py'}", "--module-parameter=numba-disable-jit=no",
        "--noinclude-pytest-mode=nofollow", "--noinclude-setuptools-mode=nofollow", "--noinclude-IPython-mode=nofollow",
        "--nofollow-import-to=*.tests", "--nofollow-import-to=tkinter", "--nofollow-import-to=tests",
        "--include-package=tps", "--include-module=tps_app",
        "--include-package=fastapi", "--include-package=starlette", "--include-package=uvicorn",
        "--include-package=pydantic", "--include-package=anyio", "--include-package=multipart",
        "--include-package=pydicom", "--include-package=pynetdicom", "--include-package=numba",
        "--include-package=llvmlite", "--include-package=contourpy", "--include-package=yaml",
        "--include-package=scipy", "--include-package=matplotlib", "--include-package=reportlab",
        "--include-package=pdfplumber", "--include-package=pdfminer", "--include-package=PIL", "--include-package=httpx",
        "--include-package-data=pydicom", "--include-package-data=pynetdicom", "--include-package-data=matplotlib",
        "--include-package-data=reportlab", "--include-package-data=pdfminer",
        f"--include-data-dir={RAIZ / 'tps' / 'app' / 'estatico'}=tps/app/estatico",
        f"--include-data-dir={RAIZ / 'dados'}=dados",
        f"--include-data-dir={RAIZ / 'machines'}=machines",
        f"--include-data-dir={RAIZ / 'templates'}=templates",
        "--enable-plugin=no-qt",
        "--product-name=TPS", f"--product-version={v}", f"--file-version={v}", "--company-name=TPS",
        "--file-description=TPS - planejamento de radioterapia (em desenvolvimento)",
        "--copyright=Todos os direitos reservados",
    ]
    if (RAIZ / "padroes").exists():
        cmd.append(f"--include-data-dir={RAIZ / 'padroes'}=padroes")
    if jobs:
        cmd.append(f"--jobs={jobs}")
    for dist in sorted(distribuicoes()):
        cmd.append(f"--include-distribution-metadata={dist}")
    if WINDOWS:
        # janela de console visível: mostra que o TPS está rodando; fechar a janela encerra o servidor
        cmd += ["--windows-console-mode=force", "--msvc=latest", f"--windows-icon-from-ico={AQUI / 'tps.ico'}"]
    cmd.append(str(RAIZ / "tps_app.py"))
    print(" ".join(cmd[:6]), "...", flush=True)
    subprocess.run(cmd, check=True, cwd=RAIZ)
    dist = SAIDA / "tps_app.dist"
    final = SAIDA / "TPS"
    if final.exists():
        shutil.rmtree(final)
    dist.rename(final)
    return final


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int)
    a = ap.parse_args()
    final = nuitka(a.jobs)
    for mod in NUMBA:
        # o numba indexa o cache de código de máquina pelo arquivo do módulo: só um marcador vai no lugar
        alvo = final / (mod.replace(".", "/") + ".py")
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_text("# Núcleo numérico compilado dentro do TPS.exe. Este arquivo só marca o local do cache.\n",
                        encoding="utf-8")
    shutil.copy2(AQUI / "tps.ico", final / "tps.ico")
    exe = final / ("TPS.exe" if WINDOWS else "TPS")
    if WINDOWS:
        copia_console(exe, final / "TPS-cmd.exe")
    vazou = [f for f in final.rglob("*") if f.suffix in (".py", ".pyc") and f.relative_to(final).parts[0] in
             ("tps", "tps_app.py") and f.stat().st_size > 300]
    if vazou:
        raise SystemExit("código-fonte ou bytecode do TPS no pacote: " + ", ".join(map(str, vazou[:10])))
    (final / "LEIA-ME.txt").write_text(
        f"TPS {versao()} — programa compilado. EM DESENVOLVIMENTO: não usar em paciente.\n\n"
        "Abra pelo atalho \"TPS\". O navegador abre em http://127.0.0.1:8770.\n"
        "Dados (usuários, auditoria, planos, exportações): C:\\ProgramData\\TPS.\n", encoding="utf-8")
    print(f"pronto: {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
