#!/bin/bash
# Grok 注册工具 Docker 构建脚本
# 用法：./scripts/docker-build.sh [--no-cache]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== 构建 Grok 注册工具 Docker 镜像 ===${NC}"

# 解析参数
NO_CACHE=""
if [ "$1" = "--no-cache" ]; then
    NO_CACHE="--no-cache"
    echo -e "${YELLOW}使用 --no-cache 模式${NC}"
fi

# 构建
docker-compose build $NO_CACHE

echo -e "${GREEN}=== 构建完成 ===${NC}"
docker images grok-register
