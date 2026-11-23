param([int]$Port = 8508, [string]$RuntimeUrl = '')

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

# 运行时不入库，首次使用自动从 Release 资产下载并解压到根目录。
if (-not $RuntimeUrl) { $RuntimeUrl = $env:RXLIB_RUNTIME_URL }
if (-not $RuntimeUrl) {
    $RuntimeUrl = 'https://gitcode.com/rxlibpub/rxlib-limitup/releases/download/py312-v1/runtime.zip'
}

function Ensure-Runtime([string]$Name, [string]$Url) {
    $py = Join-Path $Root ("runtime\{0}\python.exe" -f $Name)
    $pylibs = Join-Path $Root ("runtime\{0}\pylibs" -f $Name)
    if ((Test-Path $py) -and (Test-Path $pylibs)) { return }

    Write-Host '未检测到便携 Python 运行时，开始自动下载（约 122 MB）...' -ForegroundColor Yellow
    Write-Host "下载地址：$Url"
    $tmp = Join-Path $env:TEMP ('rxlib-runtime-{0}.zip' -f [guid]::NewGuid().ToString('N'))
    $oldPref = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try {
        Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
    } catch {
        Write-Host "运行时下载失败：$($_.Exception.Message)" -ForegroundColor Red
        Write-Host '可手动下载该 zip 并解压到本目录（生成 runtime\python312\...），再重新启动。' -ForegroundColor Yellow
        Read-Host '按回车退出'
        exit 1
    } finally {
        $ProgressPreference = $oldPref
    }
    if (-not (Test-Path $tmp) -or (Get-Item $tmp).Length -lt 50MB) {
        Write-Host '下载的运行时文件不完整，请重试。' -ForegroundColor Red
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        Read-Host '按回车退出'
        exit 1
    }

    Write-Host '正在解压运行时...'
    if (Get-Command tar.exe -ErrorAction SilentlyContinue) {
        & tar.exe -xf $tmp -C $Root
    } else {
        Expand-Archive -Path $tmp -DestinationPath $Root -Force
    }
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue

    if (-not (Test-Path $py)) {
        Write-Host "解压后仍未找到 $py，请手动解压 runtime.zip 到本目录。" -ForegroundColor Red
        Read-Host '按回车退出'
        exit 1
    }
    Write-Host '运行时准备完成。' -ForegroundColor Green
}

Ensure-Runtime $runtimeName $RuntimeUrl

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