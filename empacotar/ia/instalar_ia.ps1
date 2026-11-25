# Instala o módulo de contorno por IA do TPS (TotalSegmentator) em %ProgramData%\TPS\ia\venv.
# Precisa do Python 3.10–3.12 (python.org) e de internet (pacotes ~2 GB; pesos da rede baixados no 1º uso).
# Uso:  powershell -ExecutionPolicy Bypass -File instalar_ia.ps1 [-GPU] [-Destino <pasta de dados>\ia]
# Também é chamado pelo próprio TPS (menu Ferramentas > Instalar contorno por IA).
param([switch]$GPU, [string]$Destino = "")
$ErrorActionPreference = "Stop"
$destino = if ($Destino) { $Destino } else { Join-Path $env:ProgramData "TPS\ia" }
New-Item -ItemType Directory -Force -Path $destino | Out-Null
$py = $null
foreach ($v in @("3.12", "3.11", "3.10")) { try { & py "-$v" -c "print(1)" *> $null; if ($LASTEXITCODE -eq 0) { $py = @("py", "-$v"); break } } catch {} }
if (-not $py) { throw "Python 3.10–3.12 não encontrado. Instale de https://www.python.org (marque 'Add to PATH') e rode de novo." }
Write-Host "Criando o ambiente em $destino\venv ..."
& $py[0] $py[1] -m venv (Join-Path $destino "venv")
$pip = Join-Path $destino "venv\Scripts\pip.exe"
& $pip install --upgrade pip
if ($GPU) { & $pip install torch --index-url https://download.pytorch.org/whl/cu121 }
else { & $pip install torch --index-url https://download.pytorch.org/whl/cpu }
& $pip install TotalSegmentator rt_utils
$exe = Join-Path $destino "venv\Scripts\TotalSegmentator.exe"
if (-not (Test-Path $exe)) { throw "TotalSegmentator.exe não foi criado" }
Write-Host "Baixando os pesos da rede (uma vez)..."
& (Join-Path $destino "venv\Scripts\totalseg_download_weights.exe") -t total_fast
Write-Host "Pronto. Reabra o TPS: 'Órgãos por IA' fica disponível em Estruturas > Contorno automático."
