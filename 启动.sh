#!/bin/sh
# Linux 双击启动入口
cd "$(dirname "$0")" || exit 1
exec python3 启动.py
