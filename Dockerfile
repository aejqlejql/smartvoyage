# ============================================================
# SmartVoyage 服务镜像构建文件
#
# 作用：把整套 Python 服务（主 API + MCP + A2A）打包成一个可复用镜像，
#       供 docker-compose 启动多个容器实例（mysql / weather-mcp / ticket-mcp / ... / api）。
#       同一个镜像通过不同的启动命令（command）运行不同服务。
#
# 构建方式：在项目根目录执行 docker compose build（或 docker build -t smartvoyage .）
# ============================================================

# 基础镜像：Python 3.11 的精简版（slim 体积小，适合容器部署）
FROM python:3.11-slim

# 设置运行时环境变量：
#   PYTHONDONTWRITEBYTECODE=1  不生成 .pyc 字节码文件，减小镜像体积
#   PYTHONUNBUFFERED=1         关闭 Python 输出缓冲，日志能实时打印（容器排障必需）
#   PIP_NO_CACHE_DIR=1         pip 不写缓存，进一步减小镜像体积
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 设置容器内工作目录（后续 COPY/RUN/CMD 都基于此目录）
WORKDIR /app

# 安装系统级依赖：
#   build-essential 提供 gcc/make 等编译工具，某些 pip 包（如 mysql-connector 依赖）
#   安装时需要编译；安装完清理 apt 缓存以减小体积
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# 先单独复制 requirements.txt 再安装依赖：
#   这样能利用 Docker 层缓存——只要依赖文件不变，重新构建时无需重复安装
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip \
    && python -m pip install -r /tmp/requirements.txt

# 把整个项目源码复制到容器内的 /app/ai01_SmartVoyage 目录
COPY . /app/ai01_SmartVoyage

# 确保工作目录回到 /app，使下面的 python -m 能按包名找到模块
WORKDIR /app

# 容器启动时默认运行主 API 服务
# （docker-compose 中其他服务会用各自 command 覆盖这个默认命令）
CMD ["python", "-m", "ai01_SmartVoyage.api.server"]
