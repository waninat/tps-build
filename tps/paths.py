"""Onde ficam as coisas: programa (substituível a cada versão) × dados da clínica (preservados).

PROGRAMA  = pasta desta versão (código, kernels, espectros, modelos de planilha, padrões de fábrica).
            Pode ser apagada e trocada por uma versão nova sem perda de nada.
TPS_HOME = pasta de dados da clínica, que sobrevive às atualizações:
    config/      criterios.yaml, servidor.yaml
    machines/    máquinas cadastradas (*.yaml), models/ (ativos, pendentes, arquivo), beamdata/, ct/
    dados/       banco tps.db, exportações recebidas, resultados, PDFs, licença, código da instalação
    backups/     cópias automáticas antes de migrações e backups manuais

Ordem de escolha do TPS_HOME: variável de ambiente TPS_HOME → arquivo `tps_home.txt` na pasta
do programa (escrito pelo instalador) → padrão do sistema (Windows: %ProgramData%\\TPS; outros: ~/TPS).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PROGRAM = Path(__file__).resolve().parents[1]
FACTORY_MACHINES = PROGRAM / "machines"          # kernels/, spectra/, ct/ (curvas de exemplo)
FACTORY_CONFIG = PROGRAM / "padroes"             # padrões de fábrica (criterios.yaml, servidor.yaml)

_home: Path | None = None


def default_home() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "TPS"
    return Path.home() / "TPS"


def home() -> Path:
    global _home
    if _home is None:
        env = os.environ.get("TPS_HOME")
        marker = PROGRAM / "tps_home.txt"
        if env:
            _home = Path(env)
        elif marker.exists() and marker.read_text(encoding="utf-8").strip():
            _home = Path(marker.read_text(encoding="utf-8").strip())
        else:
            _home = default_home()
        _home = _home.expanduser().resolve()
    return _home


def set_home(p: str | Path | None) -> None:
    """Troca a pasta de dados (testes, linha de comando --home)."""
    global _home
    _home = Path(p).expanduser().resolve() if p else None


def config_dir() -> Path:
    return home() / "config"


def machines_dir() -> Path:
    return home() / "machines"


def models_dir() -> Path:
    return machines_dir() / "models"


def beamdata_dir() -> Path:
    return machines_dir() / "beamdata"


def ct_dirs() -> list[Path]:
    return [machines_dir() / "ct", FACTORY_MACHINES / "ct"]


def data_dir() -> Path:
    return home() / "dados"


def backups_dir() -> Path:
    return home() / "backups"


def kernels_file() -> Path:
    return FACTORY_MACHINES / "kernels" / "agua_edk_egsnrc.npz"


def spectra_dir() -> Path:
    return FACTORY_MACHINES / "spectra"


def ensure_home() -> Path:
    """Cria a estrutura da pasta de dados e copia os padrões de fábrica que faltarem (nunca sobrescreve)."""
    h = home()
    for d in (config_dir(), machines_dir(), models_dir(), beamdata_dir(), machines_dir() / "ct", data_dir(), backups_dir()):
        d.mkdir(parents=True, exist_ok=True)
    for name in ("criterios.yaml", "servidor.yaml"):
        dst = config_dir() / name
        if not dst.exists() and (FACTORY_CONFIG / name).exists():
            shutil.copy2(FACTORY_CONFIG / name, dst)
    return h


def to_rel(p) -> str | None:
    """Caminho guardado no banco: relativo ao TPS_HOME quando possível (a pasta pode mudar de lugar)."""
    if p is None or p == "":
        return p
    p = Path(p)
    try:
        return p.resolve().relative_to(home()).as_posix()
    except ValueError:
        return str(p)


def from_rel(p) -> str | None:
    if p is None or p == "":
        return p
    q = Path(p)
    return str(q if q.is_absolute() else home() / q)


def relocate_legacy(p: str) -> str:
    """Caminho absoluto de uma versão antiga (dados dentro da pasta do programa) → relativo ao TPS_HOME.

    Procura o último segmento 'dados' ou 'machines' e mantém dali em diante. Funciona com separadores
    do Windows e do Linux, para bancos copiados de outra máquina."""
    if not p:
        return p
    filhos = {"dados": {"verificacoes", "entrada_dicom", "entrada_manual", "entrada", "comissionamento"},
              "machines": {"models", "beamdata", "ct"}}
    parts = p.replace("\\", "/").split("/")
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] in filhos and parts[i + 1] in filhos[parts[i]]:
            return "/".join(parts[i:])
    return p
