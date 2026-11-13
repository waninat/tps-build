#!/usr/bin/env bash
# TPS — inicia o servidor local (Linux/macOS). Na primeira vez cria o ambiente e instala as dependências.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python -m tps.casos_teste casos_teste
fi
exec .venv/bin/python -m tps.app.servidor "$@"
