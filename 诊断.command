#!/bin/bash
cd "$(dirname "$0")" || exit 1

echo "════════════ 环境诊断 ════════════"
echo "目录：$PWD"
sw_vers 2>/dev/null
echo "芯片：$(uname -m)"
echo ""
echo "── Python ──"
which python3 || echo "找不到 python3"
python3 --version 2>&1
for v in python3.12 python3.11 python3.10; do
  command -v $v >/dev/null 2>&1 && echo "$v -> $($v --version 2>&1)"
done
echo ""
echo "── 运行环境 ──"
if [ -x ".venv/bin/python" ]; then
  .venv/bin/python --version
  .venv/bin/python -c "import fastapi, uvicorn, httpx, multipart; print('依赖：正常')" 2>&1 || echo "依赖：异常，删掉 .venv 后重新启动"
else
  echo "还没有创建 .venv（说明启动脚本没跑成功）"
fi
echo ""
echo "── 代码 ──"
if [ -x ".venv/bin/python" ]; then
  .venv/bin/python -c "import app; print('app.py 可以正常导入')" 2>&1
else
  python3 -c "import app; print('app.py 可以正常导入')" 2>&1
fi
echo ""
echo "── 端口 8765 ──"
lsof -nP -iTCP:8765 -sTCP:LISTEN 2>/dev/null || echo "8765 当前没有程序监听"
echo ""
echo "── 服务健康检查 ──"
curl -s -m 3 http://127.0.0.1:8765/api/health || echo "连不上：服务没启动，或端口被别的程序占了"
echo ""
echo "── 最近启动日志（最后 40 行）──"
tail -40 "启动日志.txt" 2>/dev/null || echo "还没有日志文件"
echo ""
read -r -p "按回车关闭..."