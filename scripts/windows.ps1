param(
    [ValidateSet('start', 'status', 'update', 'search')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HOME = Join-Path $projectRoot 'data\cache\huggingface'
$env:TORCH_HOME = Join-Path $projectRoot 'data\cache\torch'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

try {
    if ($Action -eq 'start' -or $Action -eq 'update') {
        if (-not [Environment]::Is64BitOperatingSystem) {
            throw 'Нужна 64-разрядная Windows.'
        }
        $toolDir = Join-Path $projectRoot '.tools'
        $uvPath = Join-Path $toolDir 'uv.exe'
        $installedUv = Get-Command uv -ErrorAction SilentlyContinue
        if ($installedUv) {
            $uvPath = $installedUv.Source
        } elseif (-not (Test-Path -LiteralPath $uvPath)) {
            Write-Host 'Устанавливаю uv и Python в папку проекта...'
            New-Item -ItemType Directory -Path $toolDir -Force | Out-Null
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $installerPath = Join-Path $toolDir 'install-uv.ps1'
            Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $installerPath
            $env:UV_UNMANAGED_INSTALL = $toolDir
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installerPath
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $uvPath)) {
                throw 'Не удалось установить uv. Передайте эту ошибку Codex.'
            }
        }
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $toolDir 'python'
        $env:UV_LINK_MODE = 'copy'
        Write-Host 'Подготавливаю программу. Первый запуск скачает зависимости и модели.'
        & $uvPath sync --frozen --python 3.12 --extra semantic --extra mcp --extra media
        if ($LASTEXITCODE -ne 0) {
            throw 'Установка зависимостей не завершена. Повторите запуск или покажите ошибку Codex.'
        }
    }
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw 'Сначала запустите Start.cmd.'
    }
    switch ($Action) {
        'start' { & $pythonPath -u -m tg_knowledge.setup }
        'status' { & $pythonPath -m tg_knowledge.cli status }
        'update' {
            & $pythonPath -u -m tg_knowledge.background --media-policy source --process-existing --prune-unused --log
        }
        'search' {
            $query = Read-Host 'Что искать в архиве'
            if ([string]::IsNullOrWhiteSpace($query)) { throw 'Запрос пустой.' }
            & $pythonPath -m tg_knowledge.cli search $query --limit 8
        }
    }
    exit $LASTEXITCODE
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
