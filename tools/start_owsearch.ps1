# 启动 open-webSearch 本地搜索守护进程（cut_agent 的联网搜索后端）
# 用法：pwsh tools/start_owsearch.ps1            （前台，默认端口 3210）
#       pwsh tools/start_owsearch.ps1 -Port 3211
param(
    [int]$Port = 3210,
    [string]$ProxyUrl = "",
    [switch]$NoProxy
)
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Join-Path $here "open-webSearch")

$useProxy = -not $NoProxy -and -not [string]::IsNullOrWhiteSpace($ProxyUrl)

if (Test-Path "build\index.js") {
    Write-Host "build 已存在，跳过构建（如需重建：npm run build）"
} else {
    Write-Host "首次运行：安装依赖并构建..."
    if ($useProxy) {
        npm ci --proxy $ProxyUrl --https-proxy $ProxyUrl --no-audit --no-fund
    } else {
        npm ci --no-audit --no-fund
    }
    if ($LASTEXITCODE -ne 0) { throw "npm ci 失败" }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build 失败" }
}

$env:PORT = "$Port"
if ($useProxy) {
    $env:USE_PROXY = "true"
    $env:PROXY_URL = $ProxyUrl
} else {
    $env:USE_PROXY = "false"
    Remove-Item Env:PROXY_URL -ErrorAction SilentlyContinue
}

# 健康检查
try {
    $h = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3
    if ($h.StatusCode -eq 200) {
        Write-Host "守护进程已在运行: http://127.0.0.1:$Port（无需重复启动）" -ForegroundColor Green
        return
    }
} catch { }

Write-Host "启动 open-webSearch daemon (port=$Port proxy=$(if($useProxy){$ProxyUrl}else{'off'}))" -ForegroundColor Cyan
node build/index.js serve
