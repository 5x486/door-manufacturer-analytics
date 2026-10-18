# 部署说明

本文档说明三种运行方式：本地开发、Docker Compose 单机部署、生产环境注意事项，以及常见故障排查。

> 状态说明：`Dockerfile`、`deploy/docker-compose.yml`、`deploy/nginx.conf` 均已提供，但**未在公网环境实测**。本文档描述的是这套配置的预期行为与部署路径，不代表已经上线运行。第一次真实部署时请按「生产环境注意项」逐条核对。

---

## 一、本地开发启动

项目自带独立 MySQL 实例（端口 3307、库 `door_analysis`、字符集 utf8mb4、数据目录 `.mysql/`），与本机 phpStudy 的 3306 实例完全隔离。所有手工步骤都收在 `tools/devops.ps1` 里。

```powershell
cd D:\python_study\pycharm_python\door_analysis_project
powershell -NoProfile -ExecutionPolicy Bypass -File tools\devops.ps1 pipeline
powershell -NoProfile -ExecutionPolicy Bypass -File tools\devops.ps1 run
# 浏览器打开 http://127.0.0.1:8000
```

### devops.ps1 子命令

| 子命令 | 作用 | 什么时候用 |
|---|---|---|
| `status` | 查看环境状态：MySQL 是否在跑、各层行数、预聚合表、最近一次质检结果 | 开工第一件事，确认环境是好的 |
| `start-db` | 启动项目自带的 MySQL 实例（端口 3307），最多等 30 秒 | 数据库没起来时单独执行 |
| `stop-db` | 停止该实例 | 收工释放内存 |
| `migrate` | 生成并应用迁移 | 改了模型之后 |
| `seed` | 灌入全部数据：8 张 CSV 导入 + 成本 / 账期 / 交付 / 人效 / 目标 / 竞品 / RFM 派生数据 | 数据被改乱，想回到干净状态 |
| `dws` | 重建 8 张预聚合表（3,736 行，约 0.75 秒） | 明细数据变了之后 |
| `check` | 跑 25 条数据质量校验，error 级失败返回非 0 | 改过数据管道之后 |
| `pipeline` | 串起来跑：起库 → 迁移 → 灌数 → 指标口径 → 建仓 → 质检 | 首次启动或换机器 |
| `run` | 启动 Django 开发服务器（http://127.0.0.1:8000） | 日常开发 |

脚本支持两个参数覆盖默认路径：

| 参数 | 默认值 | 用途 |
|---|---|---|
| `-Python` | `D:\python_study\pycharm_python\pytotal\.venv\Scripts\python.exe` | 换机器时指定解释器 |
| `-MysqlBin` | `D:\xiaopi\phpstudy_pro\Extensions\MySQL8.0.12\bin` | 换机器时指定 MySQL 二进制目录 |

四个数据管道命令也可以直接跑：

```powershell
python manage.py seed_demo       # 8 张 CSV 导入 + 派生数据
python manage.py sync_metrics    # 56 条指标口径入库
python manage.py build_dws       # 重建 8 张预聚合表
python manage.py check_quality   # 25 条数据质量校验
```

四个命令全部幂等，固定随机种子 `SEED=20260809`，重复执行结果一致。

### 演示账号

| 账号 | 密码 | 角色 |
|---|---|---|
| `admin` | `admin123456` | 老板：全部数据、可导出、可见客户联系方式 |
| `demo` | `demo123456` | 只读访客：客户电话脱敏为 `135****99` |

---

## 二、Docker Compose 部署

### 用到的文件

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 生产镜像。`python:3.12-slim` 打底，装 `default-libmysqlclient-dev`、`gcc`、`pkg-config` 编译 mysqlclient（选 slim 而不是 alpine，是因为 alpine 的 musl 会让编译产物在运行期缺库，排查成本远高于省下的几十 MB）。先拷 `requirements.txt` 装依赖再拷代码，只改业务代码时仍能命中 pip 层缓存。用非 root 用户 `appuser` 运行，暴露 8000 端口，健康检查打应用根路径 |
| `deploy/docker-compose.yml` | 单机编排两个服务。`db` 是 MySQL 8.0，建库阶段就定死 utf8mb4 与 utf8mb4_unicode_ci，数据落在命名卷 `mysql_data`，并带 `mysqladmin ping` 健康检查；`web` 等 `db` 健康后才启动，启动命令依次是 `migrate`、`collectstatic`、`gunicorn`（3 worker、超时 120 秒） |
| `deploy/nginx.conf` | 反向代理与静态文件直出，`/static/` 与 `/media/` 由 nginx 直接读卷，只把动态请求转给 `web` |
| `.env.example` | 环境变量样例，复制成 `.env` 后按需修改 |

### 需要的 .env 变量

Compose 文件里没有默认值的变量必须显式提供，否则启动会直接报错退出（这是有意设计：生产密钥不允许兜底）。

| 变量 | 必填 | 说明 |
|---|---|---|
| `MYSQL_ROOT_PASSWORD` | 是 | MySQL root 密码 |
| `MYSQL_PASSWORD` | 是 | 应用账号 `door_analysis` 的密码 |
| `DJANGO_SECRET_KEY` | 是 | Django 密钥，用随机 50 位字符串，不要复用开发环境的默认值 |
| `MYSQL_DATABASE` | 否 | 默认 `door_analysis` |
| `MYSQL_USER` | 否 | 默认 `door_analysis` |
| `DJANGO_ALLOWED_HOSTS` | 否 | 默认 `localhost,127.0.0.1` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | 否 | 默认 `http://localhost:8000` |
| `REDIS_URL` | 否 | 留空则用本地内存缓存；多进程部署建议配上 |

注意：`web` 服务读的是 `MYSQL_*` 前缀，本地开发的 `config.settings.local` 读的是 `DB_*` 前缀，两套变量不要混用。生产配置 `config.settings.production` 只认 `MYSQL_*`。

### 启动命令

```bash
# 在项目根目录
cp .env.example .env
# 编辑 .env，至少填好 MYSQL_ROOT_PASSWORD、MYSQL_PASSWORD、DJANGO_SECRET_KEY

docker compose -f deploy/docker-compose.yml --env-file .env up -d --build

# 首次启动后灌入演示数据（可选，做演示环境时执行）
docker compose -f deploy/docker-compose.yml --env-file .env exec web \
  python manage.py seed_demo
docker compose -f deploy/docker-compose.yml --env-file .env exec web \
  python manage.py sync_metrics
docker compose -f deploy/docker-compose.yml --env-file .env exec web \
  python manage.py build_dws
docker compose -f deploy/docker-compose.yml --env-file .env exec web \
  python manage.py check_quality --trigger manual
```

访问 `http://<服务器地址>:8000`。

### 健康检查

两层健康检查，分别回答不同问题：

| 层 | 检查方式 | 含义 |
|---|---|---|
| `db` | `mysqladmin ping -h 127.0.0.1 -u root -p$MYSQL_ROOT_PASSWORD --silent`，间隔 10 秒，重试 10 次，启动宽限 40 秒 | 数据库能接受连接。`web` 用 `depends_on.condition: service_healthy` 等它通过再启动，否则迁移会在数据库还没就绪时失败 |
| `web` | `curl -fsS http://127.0.0.1:8000/`，间隔 30 秒，超时 5 秒，重试 3 次（写在 Dockerfile 的 `HEALTHCHECK`） | 应用能响应请求。根路径挂的是经营总览页面，需要登录才有内容，但未登录会 302 到登录页，`curl -f` 对 302 视为成功，因此能正确表达「应用活着」 |

查看容器状态与日志：

```bash
docker compose -f deploy/docker-compose.yml ps
docker compose -f deploy/docker-compose.yml logs -f web
```

---

## 三、生产环境注意项

### Django 配置

| 项 | 要求 | 说明 |
|---|---|---|
| `DEBUG` | 必须为 `False` | 生产配置已写死 `DEBUG = False`，不受环境变量影响。`DEBUG=True` 会暴露源码路径、SQL、环境变量 |
| `SECRET_KEY` | 必须由环境变量提供，且不能与开发环境相同 | 泄露后会话与密码重置令牌可被伪造。建议至少 50 位随机字符，用 `python -c "import secrets; print(secrets.token_urlsafe(50))"` 生成 |
| `ALLOWED_HOSTS` | 必须显式提供 | 生产配置不兜底，未配置时会被 Django 拒绝服务。填真实域名，不要用 `*` |
| `CSRF_TRUSTED_ORIGINS` | 必须包含对外访问地址的完整协议与域名 | 漏配会让所有 POST 表单报 403，包括登录 |
| `SECURE_SSL_REDIRECT` | 有 HTTPS 时保持 `True` | 生产默认开启。若前面已经有 nginx 做跳转，可设为 `False` 并把 `SECURE_PROXY_SSL_HEADER` 交给 nginx 传 `X-Forwarded-Proto` |
| `SESSION_COOKIE_SECURE`、`CSRF_COOKIE_SECURE` | 保持 `True` | 只在 HTTPS 下发送 Cookie。纯 HTTP 部署时会因为浏览器不回传 Cookie 而无法登录，这是配置正确的表现，不是 bug |
| `X_FRAME_OPTIONS` | 保持 `DENY` | 经营数据看板不允许被第三方站点嵌套，防点击劫持 |

### 静态文件

生产环境由 nginx 直出，应用不处理静态资源：

- `collectstatic` 在容器启动时执行，产物落在 `/app/staticfiles`，对应命名卷 `static_data`。
- `deploy/nginx.conf` 里把 `/static/` 与 `/media/` 指向该卷，`/` 转发给 `web`。
- 生产存储类是 whitenoise 的压缩清单存储，作为 nginx 之外的兜底；清单存储要求 `collectstatic` 必须真正跑过，缺文件会直接报错而不是静默 404。
- 站点引用的是本地 `static/vendor/echarts.min.js`（1.0 MB），不依赖 CDN，内网环境可用。

### 数据库连接

- `CONN_MAX_AGE = 600` 表示连接复用 600 秒，摊薄每次请求建连的开销。数值不是越大越好：大于 MySQL 的 `wait_timeout` 会拿到已被服务端关闭的连接。
- 同时开启 `CONN_HEALTH_CHECKS = True`，复用前先探活，避免拿到失效连接。
- gunicorn 起 3 个 worker，每个 worker 各自持有连接池，所以数据库最大连接数需求约等于 worker 数加余量。3 worker 对应 2 vCPU 左右的小型部署规格，这个规模下不要盲目加到 8 个以上。
- MySQL 侧建议同时确认 `max_connections` 与 `wait_timeout`，使两者与应用设置相匹配。

### 备份策略建议

| 对象 | 方式 | 建议频率 | 备注 |
|---|---|---|---|
| MySQL 数据 | `mysqldump --single-transaction --default-character-set=utf8mb4`，或直接打包命名卷 `mysql_data` | 每日一次，保留 30 天 | `--single-transaction` 可避免锁表；导出的 SQL 必须指定 utf8mb4，否则中文会变成乱码 |
| 静态文件与媒体 | 打包 `static_data` 与 `media_data` 两个卷 | 每周一次 | 静态文件可由代码重建，媒体文件只能从卷恢复，优先级更高 |
| 日志 | 打包 `logs_data` | 每周一次，保留 30 天 | 应用日志按天滚动、保留 30 份；错误单独一份文件 |
| 配置与密钥 | `.env` 手工备份到密码管理器 | 变更时 | 不要把 `.env` 提交进版本库，`.gitignore` 已排除 |
| 备份验证 | 在测试实例上恢复一次并跑 `check_quality` | 每季度一次 | 没验证过的备份不算备份 |

数据是可重建的：`seed_demo` 加固定随机种子能生成完全相同的演示数据。因此演示环境的备份优先级低于配置与客户真实数据场景，但生产环境一旦接入真实数据，上面的频率是下限而不是建议值。

---

## 四、常见故障排查

| 现象 | 常见原因 | 排查与处理 |
|---|---|---|
| MySQL 连不上，报 `(2002, "Can't connect to ... (10061)")` | 1) 实例没启动；2) 端口写错；3) 变量名对不上 | 先跑 `powershell -File tools\devops.ps1 status` 看 3307 是否在监听。本地开发读的是 `DB_PORT`（默认 3307），生产读的是 `MYSQL_PORT`（默认 3306），两套前缀不要混用。这条错误信息会把人误导成「数据库没启动」，实际最常见的原因是配置变量名分叉 |
| 页面能打开但没有样式，静态文件 404 | 1) 没跑 `collectstatic`；2) nginx 的 `/static/` 指向目录与卷不一致；3) 生产用清单存储但清单里没有该文件 | 在容器里执行 `python manage.py collectstatic --noinput` 并确认 `/app/staticfiles` 有内容；检查 `deploy/nginx.conf` 的 `alias` 与 compose 里 `static_data` 的挂载点是否一致；浏览器强制刷新，清单存储的长缓存会让旧引用留在本地 |
| 改了模板或静态文件，页面没变化 | 1) gunicorn 默认不开模板自动重载；2) 静态文件被长缓存（30 天）；3) 改的是容器内副本而不是挂载卷 | 生产环境改模板后需 `docker compose restart web`；本地开发用 `runserver` 改模板会自动生效，改 `.py` 文件需重启；静态文件改动要重跑 `collectstatic` 并重启 |
| 中文显示为乱码或问号 | 1) 连接字符集不是 utf8mb4；2) 导出/导入时用了默认字符集；3) 终端编码不是 UTF-8 | 检查数据库连接 `OPTIONS` 里的 `charset: utf8mb4`，容器与建库参数里的 `--character-set-server=utf8mb4`；`mysqldump` 与 `mysql` 命令都要显式加 `--default-character-set=utf8mb4`；Windows 控制台是 GBK 时，管理命令里直接打印 `¥` 会抛 `UnicodeEncodeError`，用 `chcp 65001` 或改用文件日志 |
| 迁移状态不一致，提示表已存在或字段不存在 | 1) 手工改过数据库结构；2) 迁移记录表与实际结构不同步；3) 改了模型但忘记生成迁移 | 用 `python manage.py showmigrations` 对比；`python manage.py makemigrations --check --dry-run` 能发现「改了模型没生成迁移」；不要直接删表了事，先 `python manage.py sqlmigrate <app> <编号>` 看该迁移到底会执行什么；确认无误后可用 `--fake` 修正记录，但要在副本上先演练 |
| 端口被占用，服务起不来 | 3307 或 8000 已被其他进程占用 | 查看占用：`netstat -ano \| findstr :8000`（Windows）或 `ss -lntp \| grep 8000`（Linux）；本地开发常见冲突源是本机 phpStudy 的 MySQL 占着 3306，所以项目实例特意选了 3307；容器部署时确认宿主机没有别的服务占用 8000 |
| `python manage.py check` 报 `models.E020` 这类系统检查错误 | 模型字段名与 Django 内置方法重名 | 例如外键字段不能叫 `check`，因为 `Model.check()` 是框架内置方法。重命名字段并生成迁移即可，数据库列名可用 `db_column` 保持原样 |
| 登录后立刻跳回登录页（生产） | Cookie 安全属性与访问协议不匹配 | `SESSION_COOKIE_SECURE` 为 `True` 时只接受 HTTPS，用 HTTP 访问会表现成「登录不上」。给站点配上 HTTPS，或临时在受控环境下关闭这两个开关 |

---

## 五、这套配置的验证状态

为避免误解，明确区分「写过」和「跑过」：

| 项 | 状态 |
|---|---|
| 本地开发启动（`devops.ps1` 全流程） | 已实测，可跑通 |
| 四个管理命令的幂等与可复现 | 已实测，重复执行行数与金额不变 |
| 25 条数据质量校验 | 已实测，通过 24 / 警告 1 / 失败 0，约 500 毫秒 |
| 14 个页面与 21 个接口 | 已实测，页面全部返回 200 |
| 四类测试（约 40 个用例） | 已实测 |
| `Dockerfile`、`deploy/docker-compose.yml`、`deploy/nginx.conf` | **已提供但未经公网实测** |
| GitHub Actions CI 流水线 | 配置已提供，未在真实仓库上跑过完整绿线 |
| HTTPS、域名、外网访问 | 未部署，无公网链接 |
