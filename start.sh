#!/bin/sh
set -e

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -f ".venv/bin/activate" ]; then
    echo "错误：没有找到 .venv，请先创建虚拟环境："
    echo "python3 -m venv .venv"
    echo ".venv/bin/pip install -r requirements.txt"
    exit 1
fi

. .venv/bin/activate
exec python server.py
