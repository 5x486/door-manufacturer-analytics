# 生产镜像：python:3.12-slim 打底，装编译 mysqlclient 所需的系统依赖。
# 选 slim 而非 alpine 的原因：mysqlclient 需要 glibc 下的 gcc/pkg-config，
# alpine 的 musl 会让编译产物在运行期缺库，排查成本远高于省下的几十 MB。
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE：容器里不留 .pyc，镜像层更干净
# PYTHONUNBUFFERED：日志实时输出到 docker logs，不被 stdout 缓冲吞掉
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

WORKDIR /app

# 系统依赖：default-libmysqlclient-dev + gcc + pkg-config 是 mysqlclient 的编译链，
# tzdata 保证容器内时区/夏令时计算正确（报表按 Asia/Shanghai 切月）。
# 编译完立刻清掉 apt 缓存与构建产物，避免镜像无谓膨胀。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        default-libmysqlclient-dev \
        gcc \
        pkg-config \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖再拷代码，让「只改业务代码」时仍能命中 pip 层缓存
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip \
    && pip install -r /app/requirements.txt \
    && apt-get purge -y gcc \
    && apt-get autoremove -y \
    && rm -rf /root/.cache

COPY . /app

# 非 root 用户运行：容器被攻破时把影响限制在应用目录内
RUN useradd --create-home --shell /bin/bash appuser \
    && mkdir -p /app/staticfiles /app/media /app/logs \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# 健康检查走应用自身的轻量端点，供编排系统判断滚动更新是否成功
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/ || exit 1

# 3 个 worker 对应 2 vCPU 左右的小型部署（门业企业内网服务器常见规格）：
# 多了会争抢数据库连接，少了会在报表聚合时排队。
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]
