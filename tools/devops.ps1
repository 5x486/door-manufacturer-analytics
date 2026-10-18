# ============================================================================
# 开发运维脚本（Windows / PowerShell）
# ----------------------------------------------------------------------------
# 一条命令做完「起库 → 迁移 → 灌数 → 建数仓 → 跑校验」，避免每次手工敲一长串。
#
#   pwsh tools/devops.ps1 status          查看环境状态（MySQL / 数据量 / 各层行数）
#   pwsh tools/devops.ps1 start-db        启动项目自带的 MySQL（端口 3307）
#   pwsh tools/devops.ps1 stop-db         停止该实例
#   pwsh tools/devops.ps1 migrate         生成并应用迁移
#   pwsh tools/devops.ps1 seed            灌入全部数据（CSV + 成本/账期/交付/人效/目标/竞品/RFM）
#   pwsh tools/devops.ps1 dws             重建预聚合表 dws_*
#   pwsh tools/devops.ps1 check           跑数据质量校验
#   pwsh tools/devops.ps1 pipeline        上面 1~5 全部串起来跑一遍
#   pwsh tools/devops.ps1 run             启动 Django 开发服务器（http://127.0.0.1:8080）
#
# 说明：MySQL 用的是项目自带实例（.mysql/ 目录，端口 3307），
#       与你本机 phpStudy 的 3306 完全隔离，互不影响。
# ============================================================================
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('status', 'start-db', 'stop-db', 'migrate', 'seed', 'dws', 'check', 'pipeline', 'run', 'help')]
    [string]$Task = 'help',

    # 允许覆盖 Python 解释器路径（换机器时用 -Python 指定）
    [string]$Python = 'D:\python_study\pycharm_python\pytotal\.venv\Scripts\python.exe',

    # MySQL 二进制目录（phpStudy 自带；换机器时覆盖）
    [string]$MysqlBin = 'D:\xiaopi\phpstudy_pro\Extensions\MySQL8.0.12\bin'
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$IniPath = Join-Path $ProjectRoot '.mysql\my.ini'
$Port = 3307

function Write-Head($text) {
    Write-Host ''
    Write-Host ('=' * 72) -ForegroundColor DarkGray
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 72) -ForegroundColor DarkGray
}

function Write-Step($text) { Write-Host "  -> $text" -ForegroundColor Yellow }
function Write-Ok($text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Write-Err($text) { Write-Host "  [!!] $text" -ForegroundColor Red }

function Test-MysqlUp {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect('127.0.0.1', $Port)
        $client.Close()
        return $true
    }
    catch { return $false }
}

function Start-MysqlInstance {
    Write-Head "启动项目 MySQL 实例（端口 $Port）"
    if (Test-MysqlUp) { Write-Ok '已在运行，跳过启动'; return }
    if (-not (Test-Path $IniPath)) {
        Write-Err "找不到配置 $IniPath，请先初始化实例（见 docs/deployment.md）"
        exit 1
    }
    $mysqld = Join-Path $MysqlBin 'mysqld.exe'
    Start-Process -FilePath $mysqld -ArgumentList "--defaults-file=`"$IniPath`"" -WorkingDirectory $MysqlBin -WindowStyle Hidden | Out-Null
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-MysqlUp) { Write-Ok "已启动（等待 $($i + 1)s）"; return }
    }
    Write-Err "30 秒内未监听 $Port，请查看 .mysql\logs\mysqld.err"
    exit 1
}

function Stop-MysqlInstance {
    Write-Head '停止项目 MySQL 实例'
    $procs = Get-Process mysqld -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$MysqlBin*" }
    if (-not $procs) { Write-Ok '没有在运行'; return }
    $procs | Stop-Process -Force
    Write-Ok "已停止 $($procs.Count) 个进程"
}

function Ensure-Python {
    if (-not (Test-Path $Python)) {
        Write-Err "找不到 Python：$Python（用 -Python 指定）"
        exit 1
    }
}

function Invoke-Manage {
    param([string[]]$ManageArgs, [string]$Title)
    Write-Step $Title
    & $Python (Join-Path $ProjectRoot 'manage.py') @ManageArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Err "$Title 失败（退出码 $LASTEXITCODE）"
        exit $LASTEXITCODE
    }
}

function Show-Status {
    Write-Head '环境状态'
    $up = Test-MysqlUp
    Write-Host "  MySQL(3307) : $(if ($up) { '运行中' } else { '未运行' })"
    if (-not $up) {
        Write-Host '  提示：先执行 pwsh tools/devops.ps1 start-db' -ForegroundColor Yellow
        return
    }
    Ensure-Python
    $probe = @'
import os, sys, django
sys.path.insert(0, r"%ROOT%")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")
django.setup()
from django.db import connection
from apps.sales.models import Order, OrderItem, Payment, AfterSales, DeliveryStage, PaymentTerm, SalesTarget
from apps.masterdata.models import Customer, Product, Employee, CompetitorBenchmark
from apps.analytics.models import MetricDefinition, DataVersion, ModelVersion
from apps.dq.models import DQRun
from apps.core.constants import VALID_ORDER_EXCLUDE_STATUS
valid = Order.objects.exclude(order_status__in=VALID_ORDER_EXCLUDE_STATUS)
print(f"  {'订单':<12s} {Order.objects.count():>7,}   有效 {valid.count():,}")
print(f"  {'客户':<12s} {Customer.objects.count():>7,}   线索 {Customer.objects.filter(is_lead=True).count():,}")
print(f"  {'产品':<12s} {Product.objects.count():>7,}")
print(f"  {'明细':<12s} {OrderItem.objects.count():>7,}")
print(f"  {'付款':<12s} {Payment.objects.count():>7,}")
print(f"  {'售后':<12s} {AfterSales.objects.count():>7,}")
print(f"  {'账期':<12s} {PaymentTerm.objects.count():>7,}   逾期 {PaymentTerm.objects.filter(overdue_days__gt=0).count():,}")
print(f"  {'交付阶段':<12s} {DeliveryStage.objects.count():>7,}")
print(f"  {'员工/竞品/目标':<12s} {Employee.objects.count():,} / {CompetitorBenchmark.objects.count():,} / {SalesTarget.objects.count():,}")
print(f"  {'指标口径':<12s} {MetricDefinition.objects.count():>7,}")
print(f"  {'模型版本':<12s} {ModelVersion.objects.count():>7,}")
last_dq = DQRun.objects.order_by('-run_at').first()
if last_dq:
    print(f"  {'最近质检':<12s} {last_dq.run_at:%Y-%m-%d %H:%M}  通过 {last_dq.passed}/{last_dq.total_checks} 健康 {last_dq.is_healthy}")
print("  --- 预聚合表 dws_* ---")
for dv in DataVersion.objects.filter(layer="dws").order_by("table_name"):
    print(f"    {dv.table_name:<26s} {dv.row_count:>7,} 行  {dv.built_at:%Y-%m-%d %H:%M}")
'@ -replace '%ROOT%', $ProjectRoot
    $tmp = Join-Path $env:TEMP 'hbms_status.py'
    # 用 UTF-8 无 BOM 写入：带 BOM 会让 Python 读脚本首行报语法错。
    # 这里用 New-Object 而不是 [Text.UTF8Encoding]::new()，是为了兼容 Windows PowerShell 5.1。
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($tmp, $probe, $utf8NoBom)
    & $Python $tmp
}

function Invoke-Pipeline {
    Start-MysqlInstance
    Ensure-Python
    Invoke-Manage -ManageArgs @('migrate', '--skip-checks') -Title '应用数据库迁移'
    Invoke-Manage -ManageArgs @('seed_demo', '--skip-checks') -Title '灌入演示数据（CSV + 派生）'
    Invoke-Manage -ManageArgs @('sync_metrics', '--skip-checks') -Title '同步指标口径'
    Invoke-Manage -ManageArgs @('build_dws', '--skip-checks') -Title '重建预聚合表'
    Write-Head '数据质量校验'
    & $Python (Join-Path $ProjectRoot 'manage.py') check_quality --skip-checks
    Write-Ok '数据管道执行完毕'
    Show-Status
}

switch ($Task) {
    'status' { Show-Status }
    'start-db' { Start-MysqlInstance }
    'stop-db' { Stop-MysqlInstance }
    'migrate' { Start-MysqlInstance; Ensure-Python; Invoke-Manage -ManageArgs @('makemigrations', '--noinput', '--skip-checks') -Title '生成迁移'; Invoke-Manage -ManageArgs @('migrate', '--skip-checks') -Title '应用迁移' }
    'seed' { Start-MysqlInstance; Ensure-Python; Invoke-Manage -ManageArgs @('seed_demo', '--skip-checks') -Title '灌入演示数据' }
    'dws' { Start-MysqlInstance; Ensure-Python; Invoke-Manage -ManageArgs @('build_dws', '--skip-checks') -Title '重建预聚合表' }
    'check' { Start-MysqlInstance; Ensure-Python; Invoke-Manage -ManageArgs @('check_quality', '--skip-checks') -Title '数据质量校验' }
    'pipeline' { Invoke-Pipeline }
    'run' {
        Start-MysqlInstance; Ensure-Python
        Write-Head '启动开发服务器 http://127.0.0.1:8080'
        & $Python (Join-Path $ProjectRoot 'manage.py') runserver 127.0.0.1:8080 --skip-checks
    }
    default {
        Write-Head '杭博门业经营分析系统 · 开发运维脚本'
        Write-Host @'
  用法：pwsh tools/devops.ps1 <任务>

  status     查看环境状态（MySQL / 数据量 / 预聚合层 / 最近质检）
  start-db   启动项目自带 MySQL 实例（端口 3307，与 phpStudy 3306 隔离）
  stop-db    停止该实例
  migrate    生成并应用数据库迁移
  seed       灌入全部数据（CSV 导入 + 成本/账期/交付/人效/目标/竞品/RFM）
  dws        重建预聚合表 dws_*
  check      跑数据质量校验（error 级失败会返回非 0，可用于 CI）
  pipeline   串起来跑：起库 → 迁移 → 灌数 → 指标 → 数仓 → 质检
  run        启动开发服务器

  可选参数：
    -Python  <python.exe 路径>   默认用 pytotal 虚拟环境
    -MysqlBin <mysqld 所在目录>  默认用 phpStudy 自带的 MySQL 8.0.12
'@
    }
}
