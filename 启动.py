"""跨平台启动器：准备虚拟环境，后台启动本地 Web 服务并打开浏览器。"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path
from core import runtime, db

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQ = ROOT / "requirements.txt"
LOG = ROOT / "启动日志.txt"


def say(text: str) -> None:
    print(text, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def prepare_env() -> Path:
    py = venv_python()
    ok = py.exists()
    if ok:
        try:
            subprocess.run([str(py), "-c", "import fastapi, uvicorn, httpx, multipart"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            ok = False
    if not ok:
        say("首次启动或运行环境不完整，正在创建虚拟环境并安装依赖……")
        builder = venv.EnvBuilder(with_pip=True, clear=True)
        builder.create(VENV)
        subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(py), "-m", "pip", "install", "-r", str(REQ)], check=True)
    return py


def configured_port() -> int:
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        return int((cfg.get("server") or {}).get("port") or 8765)
    except (OSError, ValueError, TypeError):
        return 8765


def service_info(port: int) -> dict | None:
    try:
        # 本地请求不经过系统代理；只复用同目录的本程序，避免误开其他项目或其他服务。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=0.8) as r:
            data = json.load(r)
        if (isinstance(data, dict) and data.get("ok") and data.get("app") == runtime.APP_ID
                and Path(data.get("root") or "").resolve() == ROOT
                and Path(data.get("database") or "").resolve() == db.DB_PATH.resolve()):
            return data
    except (OSError, ValueError, TypeError, urllib.error.URLError):
        pass
    return None


def health(port: int) -> bool:
    return service_info(port) is not None


def find_running(preferred: int) -> int | None:
    for port in range(preferred, preferred + 30):
        if health(port):
            return port
    return None


def start_service(py: Path, preferred: int) -> tuple[subprocess.Popen | None, int]:
    existing = find_running(preferred)
    if existing:
        info = service_info(existing) or {}
        if info.get("code_version") == runtime.CODE_VERSION:
            return None, existing
        if info.get("busy"):
            say("检测到代码已更新，当前任务仍在运行；先打开现有服务，任务结束后再次双击启动以加载更新。")
            return None, existing
        pid = int(info.get("pid") or 0)
        if pid <= 1 or pid == os.getpid():
            raise RuntimeError("旧服务进程信息不完整，请关闭旧服务后重新启动。")
        say("检测到本目录代码已更新，正在重启空闲服务，保留配置与小说数据……")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(40):
            if not health(existing):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("旧服务尚未退出，请稍后重试，未启动重复实例。")
    LOG.write_text(f"启动时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n项目目录：{ROOT}\n", encoding="utf-8")
    log_handle = LOG.open("a", encoding="utf-8")
    kwargs: dict = {"cwd": str(ROOT), "stdin": subprocess.DEVNULL, "stdout": log_handle, "stderr": log_handle,
                    "env": {**os.environ, "NOVEL_OPEN_BROWSER": "0"}}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([str(py), "-u", str(ROOT / "app.py")], **kwargs)
    log_handle.close()
    for _ in range(80):
        port = find_running(preferred)
        if port:
            return proc, port
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    try:
        tail = "\n".join(LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-25:])
    except OSError:
        tail = ""
    raise RuntimeError(f"服务启动失败，日志最后内容：\n{tail}")


def main() -> int:
    os.chdir(ROOT)
    try:
        py = prepare_env()
        proc, port = start_service(py, configured_port())
        say(f"小说智能体已启动：http://127.0.0.1:{port}")
        say("服务已与启动窗口脱离，关闭终端不会停止服务。")
        try:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}")
        except Exception:
            pass
        return 0
    except Exception as exc:  # noqa: BLE001
        say(f"启动失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
