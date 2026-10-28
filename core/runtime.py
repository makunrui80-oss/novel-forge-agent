"""启动时固定代码版本，用于区分磁盘新代码、旧进程和其他目录的服务。"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "novel-forge-agent"


def source_version() -> str:
    digest = hashlib.sha256()
    paths = [ROOT / "app.py", *sorted((ROOT / "core").glob("*.py"))]
    for path in paths:
        digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


# 不能在 health 请求时重新计算，否则旧进程也会自称运行了磁盘上的新代码。
CODE_VERSION = source_version()


def configure_stdio() -> None:
    """Windows 重定向到日志时可能仍用 cp1252，不能让中文提示阻止服务启动。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
