@echo off
rem TPS — inicia o servidor local e abre o navegador. Na primeira vez, cria o ambiente Python e instala as dependências.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Primeira execucao: criando o ambiente Python...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Python 3.12 ou 3.13 nao encontrado. Instale em https://www.python.org/downloads/
    echo marcando "Add python.exe to PATH", e rode este arquivo de novo.
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 ( echo Falha ao instalar as dependencias. & pause & exit /b 1 )
  echo Gerando casos de teste em casos_teste\ ...
  ".venv\Scripts\python.exe" -m tps.casos_teste casos_teste
)
echo Iniciando o TPS (feche esta janela para encerrar)...
".venv\Scripts\python.exe" -m tps.app.servidor %*
pause
