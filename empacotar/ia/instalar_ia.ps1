# Instala o módulo de contorno automático de órgãos do TPS (TotalSegmentator) em <pasta de dados>\ia\venv.
# Precisa de internet (pacotes ~2 GB). Se não houver Python 3.11–3.14, instala o 3.12 pelo winget (só para o usuário).
# 3.15: ainda sem pacotes prontos para Windows de algumas dependências (scikit-image, dipy, blosc, fill-voids).
# Uso:  powershell -ExecutionPolicy Bypass -File instalar_ia.ps1 [-GPU] [-Destino <pasta de dados>\ia]
# Também é chamado pelo próprio TPS (Ferramentas > Contorno automático de órgãos).
param([switch]$GPU, [string]$Destino = "")
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$destino = if ($Destino) { $Destino } else { Join-Path $env:ProgramData "TPS\ia" }
New-Item -ItemType Directory -Force -Path $destino | Out-Null

function Rodar([string]$exe, [string[]]$argumentos, [string]$etapa) {
    Write-Host ">> $etapa"
    $ErrorActionPreference = "Continue"      # avisos do pip no stderr não são erro
    & $exe @argumentos
    $ErrorActionPreference = "Stop"
    if ($LASTEXITCODE -ne 0) { throw "Falhou: $etapa (código $LASTEXITCODE)" }
}

function Achar-Python {
    foreach ($v in @("3.12", "3.13", "3.11", "3.14")) {
        try { & py "-$v" -c "import sys" 2>$null; if ($LASTEXITCODE -eq 0) { return (& py "-$v" -c "import sys; print(sys.executable)").Trim() } } catch {}
    }
    $cands = @()
    foreach ($n in @("python", "python3")) { try { $cands += (Get-Command $n -ErrorAction Stop).Source } catch {} }
    foreach ($v in @("312", "313", "311", "314")) {
        $cands += (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe")
        $cands += (Join-Path $env:ProgramFiles "Python$v\python.exe")
    }
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c) -and ($c -notlike "*WindowsApps*")) {
            try { $ver = (& $c -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
                  if (@("3.11", "3.12", "3.13", "3.14") -contains $ver) { return $c } } catch {}
        }
    }
    return $null
}

$py = Achar-Python
if (-not $py) {
    Write-Host "Python 3.11–3.14 não encontrado. Tentando instalar o Python 3.12 pelo winget..."
    try {
        & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    } catch { Write-Host "winget indisponível: $_" }
    $py = Achar-Python
}
if (-not $py) { throw "Python 3.11–3.14 não encontrado (o 3.15 ainda não serve). Instale de https://www.python.org/downloads/ (versão 3.12, marque 'Add python.exe to PATH') e clique em Instalar de novo." }
Write-Host "Python: $py"

$venv = Join-Path $destino "venv"
Rodar $py @("-m", "venv", $venv) "Criando o ambiente em $venv"
$vpy = Join-Path $venv "Scripts\python.exe"
Rodar $vpy @("-m", "pip", "install", "--upgrade", "pip") "Atualizando o pip"
$indice = if ($GPU) { "https://download.pytorch.org/whl/cu124" } else { "https://download.pytorch.org/whl/cpu" }
Rodar $vpy @("-m", "pip", "install", "torch", "--index-url", $indice) "Instalando o PyTorch ($(if ($GPU) {'GPU NVIDIA'} else {'CPU'}); demora)"
Rodar $vpy @("-m", "pip", "install", "TotalSegmentator", "rt_utils") "Instalando o TotalSegmentator"
$exe = Join-Path $venv "Scripts\TotalSegmentator.exe"
if (-not (Test-Path $exe)) { throw "TotalSegmentator.exe não foi criado" }
$pesos = Join-Path $venv "Scripts\totalseg_download_weights.exe"
if (Test-Path $pesos) {
    Write-Host ">> Baixando os pesos da rede (uma vez)"
    & $pesos -t total_fast
    if ($LASTEXITCODE -ne 0) { Write-Host "Aviso: os pesos serão baixados no primeiro uso." }
}
Write-Host "Pronto. O contorno automático já pode ser usado (Estruturas > Contorno automático)."
