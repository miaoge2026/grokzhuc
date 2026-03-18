# Grok 注册工具 Docker 镜像
# 基于 Python 3.12，包含 Chromium、Xvfb 和所有依赖

FROM python:3.12-slim

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chromium 依赖
    chromium \
    chromium-driver \
    # 虚拟显示器
    xvfb \
    # 字体和中文支持
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    # 网络工具
    curl \
    ca-certificates \
    # 清理
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 设置 Chromium 路径
ENV CHROMIUM_BIN=/usr/bin/chromium \
    CHROMIUM_DRIVER=/usr/bin/chromedriver

# 创建工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY email_register.py .
COPY DrissionPage_example.py .
COPY config.example.json .
COPY turnstilePatch/ ./turnstilePatch/

# 创建输出目录
RUN mkdir -p /app/sso /app/logs

# 设置环境变量（可在运行时覆盖）
ENV DUCKMAIL_API_BASE=https://api.duckmail.sbs \
    DUCKMAIL_BEARER="" \
    HTTP_PROXY="" \
    HTTPS_PROXY=""

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep -f chromium || exit 1

# 入口点
ENTRYPOINT ["python", "DrissionPage_example.py"]
CMD []
