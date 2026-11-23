﻿param([int]$Port = 8508)

function Test-LocalPort([int]$TargetPort) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $TargetPort, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(1000)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppDir = Join-Path $Root 'app\current'
$ManifestPath = Join-Path $AppDir 'manifest.json'
$Entry = Join-Path $AppDir 'factor_lab_ui.py'
$HomeUrl = "http://localhost:$Port"

if (Test-LocalPort $Port) {
    Write-Host "平台已在运行：$HomeUrl（本次不再重复启动服务）" -ForegroundColor Yellow
    Start-Process $HomeUrl
    Read-Host '按回车关闭本窗口'
    exit 0
}

if (-not (Test-Path $ManifestPath)) {
    Write-Host "未找到版本清单：$ManifestPath" -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}

$manifest = Get-Content $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$runtimeName = $manifest.runtime
if (-not $runtimeName) { $runtimeName = 'python310' }

$Python = Join-Path $Root (Join-Path "runtime\$runtimeName" 'python.exe')
$Pylibs = Join-Path $Root "runtime\$runtimeName\pylibs"

if (-not (Test-Path $Python)) {
    Write-Host "未找到与本版本匹配的便携 Python：$Python" -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}
if (-not (Test-Path $Pylibs)) {
    Write-Host "未找到隔离依赖目录：$Pylibs" -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}
if (-not (Test-Path $Entry)) {
    Write-Host "未找到平台入口：$Entry" -ForegroundColor Red
    Read-Host '按回车退出'
    exit 1
}

$env:RXLIB_HOME = $Root
$env:PYTHONPATH = "$Pylibs;$AppDir"
$env:PYTHONNOUSERSITE = '1'
$env:PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION = 'python'
$env:NO_PROXY = 'localhost,127.0.0.1'
$env:no_proxy = 'localhost,127.0.0.1'

Write-Host '============================================'
Write-Host ' rxlib 量化交易研究平台'
Write-Host ' 正在启动服务，请稍候...'
Write-Host '============================================'

# 前台模式：Streamlit 直接运行在启动它的 cmd 窗口内，窗口保持常开、
# 输出实时可见；关闭窗口或按 Ctrl+C 即停止服务，重新启动只需再次双击启动脚本。

$argList = @(
    '-m', 'streamlit', 'run', $Entry,
    "--server.port=$Port",
    '--server.address=0.0.0.0',
    '--server.headless=true',
    '--global.developmentMode=false',
    '--server.fileWatcherType=none'
)
# 就绪后自动打开浏览器（后台 4 秒触发，随窗口关闭一并结束）
Start-Job -ScriptBlock { param($u) Start-Sleep -Seconds 4; Start-Process $u } -ArgumentList $HomeUrl | Out-Null

# 前台执行平台，直到服务被关闭（关窗 / Ctrl+C 即停）
& $Python @argList

Write-Host ''
Write-Host '服务已停止。'
Read-Host '按回车关闭本窗口'