#!/bin/bash
# Grok 注册工具 Docker 启动脚本
# 用法：./scripts/docker-run.sh [轮数]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}=== Grok 注册工具 Docker 启动 ===${NC}"

# 检查配置文件
if [ ! -f "config.json" ]; then
    echo -e "${YELLOW}警告：config.json 不存在，从 config.example.json 复制${NC}"
    cp config.example.json config.json
    echo -e "${YELLOW}请编辑 config.json 填写 DuckMail Bearer Token${NC}"
    exit 1
fi

# 检查必需环境变量
if [ -z "$DUCKMAIL_BEARER" ]; then
    echo -e "${RED}错误：DUCKMAIL_BEARER 环境变量未设置${NC}"
    echo "用法："
    echo "  export DUCKMAIL_BEARER=your_token_here"
    echo "  ./scripts/docker-run.sh [轮数]"
    exit 1
fi

# 注册轮数（可选参数）
COUNT="${1:-}"
if [ -n "$COUNT" ]; then
    echo -e "${GREEN}注册轮数：$COUNT${NC}"
    export REGISTER_COUNT="$COUNT"
fi

# 构建镜像（如果不存在）
if ! docker images grok-register | grep -q grok-register; then
    echo -e "${YELLOW}首次构建 Docker 镜像...${NC}"
    docker-compose build
fi

# 运行容器
echo -e "${GREEN}启动容器...${NC}"
docker-compose up --remove-orphans

echo -e "${GREEN}=== 完成 ===${NC}"
echo -e "SSO token: ${GREEN}sso/${NC}"
echo -e "运行日志：${GREEN}logs/${NC}"
