"""有界并发任务队列；同一项目串行，不同项目可以并行。"""
from __future__ import annotations

import asyncio
import os

from . import db, pipeline

_workers: list[asyncio.Task] = []
_worker_lock = None


def _acquire_worker_lock():
    """只允许一个服务进程负责队列，避免重复启动时重复恢复/执行任务。"""
    global _worker_lock
    path = db.DATA_DIR / "task-worker.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if path.stat().st_size == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        return False
    _worker_lock = handle
    return True


def recover() -> None:
    """队列锁持有者启动时，接回上次异常退出留下的运行中任务。"""
    db.execute("UPDATE tasks SET status='paused', message='上次退出时中断，可点继续' "
               "WHERE status='running'")


def start() -> None:
    global _workers
    if _workers and any(not worker.done() for worker in _workers):
        return
    if not _acquire_worker_lock():
        # 另一个本地服务实例已在处理队列；当前实例仍可提供网页/API。
        return
    recover()
    count = max(1, min(4, int(os.environ.get("NOVEL_TASK_WORKERS", "2"))))
    _workers = [asyncio.create_task(_loop(), name=f"novel-task-worker-{i + 1}")
                for i in range(count)]


async def stop() -> None:
    global _workers, _worker_lock
    for worker in _workers:
        worker.cancel()
    if _workers:
        await asyncio.gather(*_workers, return_exceptions=True)
    _workers = []
    if _worker_lock is not None:
        try:
            if os.name == "nt":
                import msvcrt
                _worker_lock.seek(0)
                msvcrt.locking(_worker_lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(_worker_lock.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        _worker_lock.close()
        _worker_lock = None


async def _loop() -> None:
    while True:
        try:
            row = db.claim_pending_task()
            if not row:
                await asyncio.sleep(0.5)
                continue
            await _run(row)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            await asyncio.sleep(1)


async def _run(row: dict) -> None:
    tid = row["id"]
    try:
        await pipeline.dispatch(row)
        fresh = db.task_row(tid) or {}
        if fresh.get("status") == "running":
            db.update_task(tid, status="done", message=fresh.get("message") or "完成")
    except pipeline.Paused:
        current = db.task_status(tid)
        if current == "canceled":
            db.update_task(tid, status="canceled", message="已取消")
        else:
            db.update_task(tid, status="paused", message="已暂停")
    except asyncio.CancelledError:
        db.update_task(tid, status="paused", message="服务停止")
        raise
    except Exception as exc:  # noqa: BLE001
        db.update_task(tid, status="failed", error=str(exc)[:1200], message="失败")


def enqueue(project_id: int, type_: str, title: str, payload: dict, total: int = 0) -> int:
    return db.create_task(project_id, type_, title, payload, total)
