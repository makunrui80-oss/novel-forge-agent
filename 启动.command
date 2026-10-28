#!/bin/bash
# macOS 双击启动入口
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/"
  exit 1
fi
if [ ! -f "启动.py" ]; then
  echo "请把启动.command 放回项目文件夹；桌面快捷入口请使用 Finder 的‘制作替身’。"
  exit 1
fi
exec python3 启动.py
