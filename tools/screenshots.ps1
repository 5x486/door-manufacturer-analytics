[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8080',
    [string]$SessionId = '',
    [string]$OutDir = '',
    [int]$Width = 1600,
    [int]$Height = 1050,
    [switch]$Dark
)
# 注意：这里刻意使用 Continue。Edge 在无头模式下会往 stderr 打一堆与截图无关的噪音
# （QQBrowser 数据路径、ShaderCache 等），一旦是 Stop 就会把整个脚本判为失败，
# 而实际上截图可能已经成功。判断成功的依据只能是「文件是否真的生成」。
$ErrorActionPreference = 'Continue'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $ProjectRoot 'outputs\screenshots' }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$browser = @(
    'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
    'C:\Program Files\Google\Chrome\Application\chrome.exe',
    'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { Write-Host '未找到 Edge/Chrome'; exit 1 }

if (-not $SessionId) {
    $sidFile = Join-Path $ProjectRoot 'outputs\.sessionid'
    if (Test-Path $sidFile) { $SessionId = (Get-Content $sidFile -Raw).Trim() }
}
if (-not $SessionId) { Write-Host '缺少 sessionid'; exit 1 }

$pages = @(
    @{ path = '/';            name = '01_经营总览' },
    @{ path = '/growth/';     name = '02_增长与渠道' },
    @{ path = '/profit/';     name = '03_品类与利润' },
    @{ path = '/customers/';  name = '04_客户价值' },
    @{ path = '/retention/';  name = '05_留存与预警' },
    @{ path = '/delivery/';   name = '06_交付与售后' },
    @{ path = '/cashflow/';   name = '07_回款与现金流' },
    @{ path = '/workforce/';  name = '08_人效与门店' },
    @{ path = '/simulator/';  name = '09_决策模拟' },
    @{ path = '/dq/';         name = '10_数据质量' },
    @{ path = '/model/';      name = '11_模型在线' },
    @{ path = '/metrics/';    name = '12_指标字典' },
    @{ path = '/orders/';     name = '13_明细数据' }
)

$profileDir = Join-Path $env:TEMP ('hbms-shot-' + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null
$suffix = if ($Dark) { '_dark' } else { '' }

$ok = 0
foreach ($page in $pages) {
    $url = "$BaseUrl$($page.path)"
    $out = Join-Path $OutDir ($page.name + $suffix + '.png')
    if (Test-Path $out) { Remove-Item $out -Force }

    $args = @(
        '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
        '--disable-extensions', '--disable-sync', '--hide-scrollbars', '--force-device-scale-factor=1',
        "--window-size=$Width,$Height",
        "--user-data-dir=$profileDir",
        "--virtual-time-budget=6000",
        "--screenshot=$out"
    )
    if ($Dark) { $args += '--force-dark-mode' }
    $args += $url

    & $browser @args *> $null

    # 等文件落盘（Edge 进程退出与文件写完之间有短暂时间差）
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 300
        if ((Test-Path $out) -and ((Get-Item $out).Length -gt 10000)) { break }
    }
    if ((Test-Path $out) -and ((Get-Item $out).Length -gt 10000)) {
        $kb = [math]::Round((Get-Item $out).Length / 1KB)
        Write-Host ("  [OK] {0,-14} {1,6} KB" -f $page.name, $kb) -ForegroundColor Green
        $ok++
    } else {
        Write-Host ("  [!!] {0,-14} 未生成" -f $page.name) -ForegroundColor Red
    }
}
Remove-Item $profileDir -Recurse -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "截图完成：$ok / $($pages.Count)" -ForegroundColor Cyan