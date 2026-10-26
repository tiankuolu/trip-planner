param([switch]$InstallDependencies, [switch]$SkipBuild)
$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($InstallDependencies) {
        py -3.11 -m pip install -r backend/requirements.txt
        if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
    }
    if (-not $SkipBuild) {
        Push-Location -LiteralPath (Join-Path $PSScriptRoot 'frontend-react')
        try {
            if (-not (Test-Path -LiteralPath 'node_modules')) { npm ci --no-audit --no-fund; if ($LASTEXITCODE -ne 0) { throw 'React 依赖安装失败。' } }
            npm run build
            if ($LASTEXITCODE -ne 0) { throw 'React 页面构建失败。' }
        }
        finally { Pop-Location }
    }
    if (-not (Test-Path -LiteralPath 'frontend-react/dist/index.html')) { throw '请先构建 React 页面。' }
    Push-Location -LiteralPath (Join-Path $PSScriptRoot 'backend')
    try {
        Write-Host '智能旅行工作台：http://127.0.0.1:8772'
        py -3.11 -m uvicorn app.api.main:app --host 127.0.0.1 --port 8772
        if ($LASTEXITCODE -ne 0) { throw '旅行规划服务已异常终止。' }
    }
    finally { Pop-Location }
}
finally { Pop-Location }
