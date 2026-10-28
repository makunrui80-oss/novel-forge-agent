"""小说智能体 · 本地 Web 服务。"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sqlite3
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from core import config, db, llm, pipeline, tasks, zhuque, runtime
from core.splitter import decode
from core import utils

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    tasks.start()
    try:
        yield
    finally:
        await tasks.stop()


app = FastAPI(title="小说智能体", version="1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.exception_handler(ValueError)
@app.exception_handler(llm.LLMError)
async def value_error_handler(_, exc: Exception):  # noqa: ANN001
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(sqlite3.OperationalError)
async def sqlite_error_handler(_, exc: sqlite3.OperationalError):  # noqa: ANN001
    """把数据库异常转成可读提示，避免前端只显示 Internal Server Error。"""
    message = str(exc)
    if "no such table" in message or "file is not a database" in message:
        message = "本地数据库未正确加载。请关闭重复启动的窗口，再双击“启动.command”重新打开；启动脚本会自动从备份恢复。"
    elif "locked" in message:
        message = "本地数据库正在被另一个启动窗口占用。请只保留一个智能体窗口后重试。"
    return JSONResponse(status_code=503, content={"detail": message})


@app.exception_handler(Exception)
async def unexpected_error_handler(_, exc: Exception):  # noqa: ANN001
    """保证单个按钮异常不会让前端只看到无信息的 500。"""
    return JSONResponse(status_code=500, content={"detail": f"服务器处理失败：{exc}"})


@app.get("/")
async def index():
    return FileResponse(str(STATIC / "index.html"), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
    })


# ---------------------------------------------------------------- 配置

@app.get("/api/config")
async def get_config():
    cfg = config.load()
    spent = db.q1("SELECT COALESCE(SUM(cost),0) AS c FROM usage_log")["c"]
    return {"config": cfg, "spent": round(spent or 0, 4)}


@app.put("/api/config")
async def put_config(payload: dict = Body(...)):
    cfg = payload.get("config") or payload
    saved = config.save(cfg)
    return {"config": saved}


@app.get("/api/usage")
async def usage(project_id: int | None = None):
    where, args = "", ()
    if project_id:
        where, args = "WHERE project_id=?", (project_id,)
    rows = db.q(f"SELECT * FROM usage_log {where} ORDER BY id DESC LIMIT 100", args)
    total = db.q1(f"SELECT COALESCE(SUM(cost),0) AS cost, COALESCE(SUM(prompt_tokens),0) AS pt, "
                  f"COALESCE(SUM(completion_tokens),0) AS ct FROM usage_log {where}", args)
    return {"rows": rows, "total": total}


@app.post("/api/zhuque/test")
async def test_zhuque(payload: dict = Body(default={})):
    zone = dict(payload.get("settings") or config.load().get("zhuque", {}))
    zone.update(enabled=True, mode="api")
    # 固定测试文本，不上传项目正文；仅检测，不触发章节改写。
    text = "雨停以后，街角的早餐铺才开门。老板把椅子从桌上搬下来，问我今天还是老样子吗。我点了点头，却发现墙上的旧挂钟已经停了三天。" * 8
    return await zhuque.detect(text, settings=zone)


@app.post("/api/test_model")
async def test_model(payload: dict = Body(...)):
    provider_id = payload.get("provider_id") or ""
    if not provider_id:
        raise ValueError("请选择供应商")
    provider = config.get_provider(provider_id)
    if not provider:
        raise ValueError("供应商不存在，请先保存设置")
    model = (payload.get("model") or "").strip()
    if not model:
        models = [m.strip() for m in (provider.get("models") or []) if str(m).strip()]
        if not models:
            raise ValueError("请先在「模型列表」里填至少一个模型名，再测试接通")
        model = models[0]
    started = time.time()
    text = await llm.test_provider(provider_id, model)
    return {"ok": True, "text": text, "model": model,
            "latency": round(time.time() - started, 2)}


# ---------------------------------------------------------------- 项目

@app.get("/api/projects")
async def list_projects():
    rows = db.q("SELECT * FROM projects ORDER BY id DESC")
    for r in rows:
        r.pop("meta", None)
        ol = db.q1("SELECT content FROM outline WHERE project_id=?", (r["id"],))
        r["new_title"] = pipeline.new_book_title(r["id"]) if ol and ol["content"] else ""
    return {"projects": rows}


@app.post("/api/projects")
async def create_project(name: str = Form(""), text: str = Form(""),
                         file: UploadFile | None = File(None)):
    if file is not None:
        data = await file.read()
        content = decode(data)
        if not name:
            name = Path(file.filename or "未命名").stem
    else:
        content = text
    if not content.strip():
        raise ValueError("请上传 TXT 或粘贴正文内容。")
    name = (name or "未命名作品").strip()[:60]
    pid = pipeline.create_project(name, content, file.filename if file else "粘贴文本")
    return {"project_id": pid}


@app.delete("/api/projects/{pid}")
async def delete_project(pid: int):
    for table in ("chapters", "batches", "assets", "outline", "constitution", "generated",
                  "contexts", "tasks", "usage_log"):
        db.execute(f"DELETE FROM {table} WHERE project_id=?", (pid,))
    db.execute("DELETE FROM projects WHERE id=?", (pid,))
    return {"ok": True}


@app.put("/api/projects/{pid}")
async def rename_project(pid: int, payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise ValueError("书名不能为空")
    db.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, db.now(), pid))
    pipeline.set_meta(pid, {"reference_title": name})
    return {"ok": True}


@app.get("/api/projects/{pid}/overview")
async def overview(pid: int):
    p = pipeline.project(pid)
    batch_size = int(p["meta"].get("batch_size") or 5)
    chapters = db.q1("SELECT COUNT(*) AS c, COALESCE(SUM(word_count),0) AS w FROM chapters WHERE project_id=?",
                     (pid,))
    detail = db.q("SELECT batch_idx, status, error FROM batches WHERE project_id=? AND kind='detail' ORDER BY batch_idx",
                  (pid,))
    write = db.q("SELECT batch_idx, status, error FROM batches WHERE project_id=? AND kind='write' ORDER BY batch_idx",
                 (pid,))
    gen = db.q1("SELECT COUNT(*) AS c, COALESCE(SUM(LENGTH(content)),0) AS w FROM generated "
                "WHERE project_id=? AND content!=''", (pid,))
    scores = db.q1("SELECT AVG(zhuque_score) AS avg_score, MAX(zhuque_score) AS max_score FROM generated "
                  "WHERE project_id=? AND zhuque_score IS NOT NULL", (pid,))
    outline = db.q1("SELECT status, LENGTH(content) AS len FROM outline WHERE project_id=?", (pid,))
    cons = db.q1("SELECT locked, LENGTH(content) AS len FROM constitution WHERE project_id=?", (pid,))
    plan = []
    if outline:
        row = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
        plan = utils.parse_plan((row or {}).get("content", ""))
    detail_available = 0
    for asset in db.q("SELECT key,content FROM assets WHERE project_id=? AND key LIKE 'detail_%'", (pid,)):
        if not re.fullmatch(r"detail_\d+", asset["key"]):
            continue
        try:
            data = json.loads(asset["content"])
            detail_available += int(any(isinstance(ch, dict) and ch.get("summary")
                                        for ch in data.get("chapters", [])))
        except (ValueError, TypeError, AttributeError):
            pass
    return {
        "project": {k: v for k, v in p.items() if k != "meta"},
        "meta": p["meta"],
        "titles": {
            "reference": pipeline.reference_title(pid),
            "new": pipeline.new_book_title(pid) if plan else "",
        },
        "stats": {
            "source_chapters": chapters["c"],
            "source_words": chapters["w"],
            "batch_size": batch_size,
            "detail_total": len(detail),
            "detail_done": sum(1 for b in detail if b["status"] == "done"),
            "detail_available": detail_available,
            "plan_total": len(plan),
            "written": gen["c"],
            "written_words": gen["w"],
            "avg_zhuque": round(scores["avg_score"], 1) if scores and scores["avg_score"] else None,
            "max_zhuque": scores["max_score"] if scores else None,
            "outline_status": (outline or {}).get("status"),
            "constitution_locked": bool((cons or {}).get("locked")),
        },
        "batches": {"detail": detail, "write": write},
        "warnings": (p["meta"].get("warnings") or [])[-10:],
    }


@app.get("/api/projects/{pid}/source/{idx}")
async def source_chapter(pid: int, idx: int):
    row = db.q1("SELECT * FROM chapters WHERE project_id=? AND idx=?", (pid, idx))
    if not row:
        raise HTTPException(404, "章节不存在")
    return row


@app.get("/api/projects/{pid}/chapters")
async def chapter_list(pid: int):
    rows = db.q("SELECT idx, title, word_count, batch_idx FROM chapters WHERE project_id=? ORDER BY idx",
                (pid,))
    return {"chapters": rows}


# ---------------------------------------------------------------- 拆书

@app.post("/api/projects/{pid}/analyze")
async def start_analyze(pid: int, payload: dict = Body(default={})):
    pipeline.project(pid)
    config.resolve_stage("analyze")  # 没配模型就直接报错，不建任务
    pipeline.set_meta(pid, {"analysis_finished": False})
    mode = payload.get("mode", "next")
    batch = payload.get("batch")
    title = "拆书细纲"
    if mode == "all":
        title = "拆书：自动跑完剩余批次"
    elif batch is not None:
        title = f"拆书：第 {int(batch) + 1} 批"
    else:
        title = "拆书：继续下一批"
    tid = db.create_task(pid, "analyze", title, {"mode": mode, "batch": batch})
    return {"task_id": tid}


@app.post("/api/projects/{pid}/analyze/finish")
async def finish_analyze(pid: int):
    pipeline.finish_analysis(pid)
    return {"ok": True, "message": "当前正在处理的批次落库后结束拆书；可用已有细纲生成大纲。"}


@app.get("/api/projects/{pid}/assets")
async def assets(pid: int):
    rows = db.q("SELECT key, content FROM assets WHERE project_id=?", (pid,))
    out: dict = {"details": [], "archive": None, "modules": None, "raw": {},
                 "preflight": None}
    for r in rows:
        key = r["key"]
        try:
            val = json.loads(r["content"])
        except Exception:
            val = r["content"]
        if key.startswith("detail_"):
            if key.endswith("_raw"):
                out["raw"][key] = r["content"][:2000]
            else:
                try:
                    b = int(key.split("_")[1])
                except Exception:
                    continue
                out["details"].append({"batch": b, "data": val})
        elif key == "archive":
            out["archive"] = val
        elif key == "modules":
            out["modules"] = val
        elif key == "preflight":
            out["preflight"] = val
    out["details"].sort(key=lambda x: x["batch"])
    out["quality_report"] = pipeline.quality_report(pid)
    # 质量报告由数据库状态实时计算，同时落一份 asset，方便备份和排查旧项目。
    db.upsert_asset(pid, "quality_report", json.dumps(out["quality_report"], ensure_ascii=False))
    return out


@app.get("/api/projects/{pid}/contexts")
async def contexts(pid: int):
    rows = db.q("SELECT after_batch, content, deviation, updated_at FROM contexts WHERE project_id=? ORDER BY after_batch",
                (pid,))
    for r in rows:
        try:
            r["data"] = json.loads(r["content"])
        except Exception:
            r["data"] = {"context_card": r["content"]}
    return {"contexts": rows}


# ---------------------------------------------------------------- 大纲 / 宪法

@app.get("/api/projects/{pid}/outline")
async def get_outline(pid: int):
    row = db.q1("SELECT * FROM outline WHERE project_id=?", (pid,))
    plan = utils.parse_plan((row or {}).get("content", ""))
    return {"outline": row, "plan": plan}


@app.post("/api/projects/{pid}/outline/plan")
async def repair_outline_plan(pid: int):
    pipeline.project(pid)
    row = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    if not row or not row["content"].strip():
        raise ValueError("请先生成新书大纲，可使用部分拆书结果。")
    if not utils.parse_plan(row["content"]):
        config.resolve_stage("outline")
    pipeline.finish_analysis(pid)
    tid = db.create_task(pid, "plan", "补提取章节计划", {}, total=1)
    return {"task_id": tid}


@app.put("/api/projects/{pid}/outline")
async def put_outline(pid: int, payload: dict = Body(...)):
    content = payload.get("content")
    if content is None:
        raise ValueError("缺少大纲内容")
    db.execute(
        """INSERT INTO outline(project_id, requirement, content, status, updated_at) VALUES(?,?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET content=excluded.content, status='edited',
             updated_at=excluded.updated_at""",
        (pid, payload.get("requirement", ""), content, "edited", db.now()),
    )
    return {"ok": True, "plan": utils.parse_plan(content)}


@app.post("/api/projects/{pid}/outline/generate")
async def generate_outline(pid: int, payload: dict = Body(...)):
    pipeline.project(pid)
    config.resolve_stage("outline")
    requirement = payload.get("requirement") or ""
    if not requirement.strip():
        raise ValueError("请先填写原创要求（书名、题材、主角、核心看点等）。")
    pipeline.finish_analysis(pid)
    # 先保存要求，方便任务失败后重试
    db.execute(
        """INSERT INTO outline(project_id, requirement, content, status, updated_at) VALUES(?,?,'','writing',?)
           ON CONFLICT(project_id) DO UPDATE SET requirement=excluded.requirement,
             status='writing', updated_at=excluded.updated_at""",
        (pid, requirement, db.now()),
    )
    tid = db.create_task(pid, "outline", "生成原创大纲 + 创作宪法",
                         {"requirement": requirement}, total=3)
    return {"task_id": tid}


@app.get("/api/projects/{pid}/constitution")
async def get_constitution(pid: int):
    row = db.q1("SELECT * FROM constitution WHERE project_id=?", (pid,))
    return {"constitution": row}


@app.put("/api/projects/{pid}/constitution")
async def put_constitution(pid: int, payload: dict = Body(...)):
    row = db.q1("SELECT * FROM constitution WHERE project_id=?", (pid,))
    content = payload.get("content", (row or {}).get("content", ""))
    locked = bool(payload.get("locked", (row or {}).get("locked", 0)))
    db.execute(
        """INSERT INTO constitution(project_id, content, locked, updated_at) VALUES(?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET content=excluded.content, locked=excluded.locked,
             updated_at=excluded.updated_at""",
        (pid, content, 1 if locked else 0, db.now()),
    )
    return {"ok": True, "locked": locked}


@app.post("/api/projects/{pid}/constitution/generate")
async def gen_constitution(pid: int):
    pipeline.project(pid)
    text = await pipeline.regen_constitution(pid)
    return {"content": text}


# ---------------------------------------------------------------- 写作

@app.post("/api/projects/{pid}/write")
async def start_write(pid: int, payload: dict = Body(default={})):
    pipeline.project(pid)
    config.resolve_stage("write")
    pipeline._get_constitution(pid)  # 提前校验，前端能立刻看到错误
    pipeline.finish_analysis(pid)
    mode = payload.get("mode", "next")
    batch = payload.get("batch")
    title = {"next": "写作：继续下一批（5章）", "all": "写作：自动写完"}.get(mode, "写作")
    if batch is not None:
        title = f"写作：重新生成第 {int(batch) + 1} 批"
    tid = db.create_task(pid, "write", title,
                         {"mode": mode, "batch": batch, "force": bool(payload.get("force"))})
    return {"task_id": tid}


@app.get("/api/projects/{pid}/generated")
async def generated_list(pid: int, full: int = 0):
    rows = db.q("SELECT * FROM generated WHERE project_id=? ORDER BY idx", (pid,))
    out = []
    for r in rows:
        item = {
            "id": r["id"], "idx": r["idx"], "title": r["title"], "status": r["status"],
            "zhuque_score": r["zhuque_score"], "batch_idx": r["batch_idx"],
            "words": utils.word_count(r["content"]),
            "has_polished": bool(r["polished"]),
        }
        item["excerpt"] = (r["content"] or "")[:120]
        if full:
            item["content"] = r["content"]
            item["polished"] = r["polished"]
        out.append(item)
    return {"chapters": out}


@app.get("/api/generated/{gid}")
async def generated_one(gid: int):
    row = db.q1("SELECT * FROM generated WHERE id=?", (gid,))
    if not row:
        raise HTTPException(404, "章节不存在")
    for name, key in (("detection", f"zhuque_{row['idx']}_latest"),):
        try:
            row[name] = json.loads(db.get_asset(row["project_id"], key) or "null")
        except (ValueError, TypeError):
            row[name] = None
    if row.get("detection"):
        import hashlib
        row["detection"]["matches_current"] = row["detection"].get("text_sha256") == hashlib.sha256(
            pipeline._chapter_text(row).encode("utf-8")).hexdigest()
    return row


@app.put("/api/generated/{gid}")
async def update_generated(gid: int, payload: dict = Body(...)):
    row = db.q1("SELECT * FROM generated WHERE id=?", (gid,))
    if not row:
        raise HTTPException(404, "章节不存在")
    title = payload.get("title", row["title"])
    content = payload.get("content", row["content"])
    polished = payload.get("polished", row["polished"])
    if content != row["content"] and "polished" not in payload:
        polished = ""  # 旧优化稿基于旧正文，不能继续用于前文记忆或导出。
    changed = content != row["content"] or polished != row["polished"]
    if changed:
        pipeline._backup_chapter(row["project_id"], row["idx"], row, 0, "manual_edit")
        pipeline._invalidate_contexts(row["project_id"], row["idx"])
    db.execute("UPDATE generated SET title=?, content=?, polished=?, zhuque_score=?, status=?, updated_at=? WHERE id=?",
               (title, content, polished, None if changed else row["zhuque_score"],
                "draft" if changed else row["status"], db.now(), gid))
    return {"ok": True}


@app.post("/api/projects/{pid}/check")
async def check_chapters(pid: int, payload: dict = Body(default={})):
    zone = config.load().get("zhuque", {})
    if not zone.get("enabled"):
        raise ValueError("朱雀检测还没启用，请先在设置里开启并配置接口")
    if zone.get("mode") != "manual":
        error = zhuque.validate_settings(zone)
        if error:
            raise ValueError(error)
    idxs = payload.get("idxs") or []
    tid = db.create_task(pid, "check", f"朱雀检测（{len(idxs) or '全部'}章）", {"idxs": idxs})
    return {"task_id": tid}


@app.post("/api/projects/{pid}/rewrite")
async def rewrite(pid: int, payload: dict = Body(...)):
    config.resolve_stage("polish")
    idx = int(payload.get("idx") or 0)
    tid = db.create_task(pid, "rewrite", f"重写第 {idx} 章",
                         {"idx": idx, "instruction": payload.get("instruction") or ""})
    return {"task_id": tid}


@app.post("/api/projects/{pid}/polish")
async def polish(pid: int, payload: dict = Body(default={})):
    config.resolve_stage("polish")
    tid = db.create_task(pid, "polish", "全书优化", payload)
    return {"task_id": tid}


@app.post("/api/projects/{pid}/context")
async def rebuild_context(pid: int, payload: dict = Body(...)):
    config.resolve_stage("check")
    b = int(payload.get("batch") or 0)
    tid = db.create_task(pid, "context", f"重建第 {b + 1} 批承接卡", {"batch": b})
    return {"task_id": tid}


@app.post("/api/projects/{pid}/realign")
async def realign_deviations(pid: int, payload: dict = Body(...)):
    """按整本主题重写已定位偏离章节，再重新生成承接卡。"""
    pipeline.project(pid)
    config.resolve_stage("polish")
    b = int(payload.get("batch") or 0)
    tid = db.create_task(pid, "realign", f"主题校准并重写第 {b + 1} 批偏离章节", {"batch": b})
    return {"task_id": tid}


@app.put("/api/generated/{gid}/score")
async def record_score(gid: int, payload: dict = Body(...)):
    row = db.q1("SELECT * FROM generated WHERE id=?", (gid,))
    if not row:
        raise HTTPException(404, "章节不存在")
    try:
        score = float(payload.get("score"))
    except (TypeError, ValueError):
        raise ValueError("检测分必须是数字")
    threshold = float(config.load()["zhuque"].get("pass_threshold") or 20)
    status = "checked" if score <= threshold else "high_aigc"
    db.execute("UPDATE generated SET zhuque_score=?, status=?, updated_at=? WHERE id=?",
               (score, status, db.now(), gid))
    return {"ok": True, "score": score, "pass": score <= threshold}


@app.get("/api/projects/{pid}/export")
async def export(pid: int, fmt: str = "txt", polished: int = 1):
    text = pipeline.export_text(pid, use_polished=bool(polished))
    safe = re.sub(r"[\\/:*?\"<>|]", "_", pipeline.new_book_title(pid))[:40]
    if fmt == "md":
        body = text
        media, ext = "text/markdown; charset=utf-8", "md"
    else:
        body = text.replace("## ", "")
        media, ext = "text/plain; charset=utf-8", "txt"
    filename = quote(f"{safe}_成稿.{ext}")
    return Response(content=body.encode("utf-8"), media_type=media,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})


# ---------------------------------------------------------------- 任务

@app.get("/api/tasks")
async def list_tasks(project_id: int | None = None, limit: int = 60):
    if project_id:
        rows = db.q("SELECT * FROM tasks WHERE project_id=? ORDER BY id DESC LIMIT ?", (project_id, limit))
    else:
        rows = db.q("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        try:
            r["payload"] = json.loads(r.get("payload") or "{}")
        except Exception:
            pass
    return {"tasks": rows}


@app.post("/api/tasks/{tid}/pause")
async def pause_task(tid: int):
    row = db.task_row(tid)
    if not row:
        raise HTTPException(404, "任务不存在")
    if row["status"] in ("pending", "running", "paused"):
        db.update_task(tid, status="paused", message="已暂停")
    return {"ok": True}


@app.post("/api/tasks/{tid}/resume")
async def resume_task(tid: int):
    row = db.task_row(tid)
    if not row:
        raise HTTPException(404, "任务不存在")
    stage = {"analyze": "analyze", "outline": "outline", "plan": "outline", "write": "write",
             "polish": "polish", "rewrite": "polish", "context": "check",
             "check": "check"}.get(row["type"])
    if stage:
        config.resolve_stage(stage)
    if row["status"] in ("paused", "failed", "canceled"):
        if row["type"] == "analyze":
            pipeline.set_meta(row["project_id"], {"analysis_finished": False})
        db.update_task(tid, status="pending", error="", message="继续执行")
    return {"ok": True}


@app.post("/api/tasks/{tid}/cancel")
async def cancel_task(tid: int):
    row = db.task_row(tid)
    if not row:
        raise HTTPException(404, "任务不存在")
    db.update_task(tid, status="canceled", message="已取消")
    return {"ok": True}


@app.post("/api/projects/{pid}/continue")
async def continue_task(pid: int):
    """有大纲时优先写作；选择结束拆书后不再自动补拆剩余章节。"""
    active = db.q1("SELECT id, title FROM tasks WHERE project_id=? AND status IN ('running','pending') "
                   "ORDER BY id DESC LIMIT 1", (pid,))
    if active:
        return {"task_id": active["id"], "mode": "running",
                "message": f"任务 #{active['id']}「{active['title']}」正在执行"}
    p = pipeline.project(pid)
    row = db.q1("SELECT * FROM tasks WHERE project_id=? AND status IN ('paused','failed') "
                "ORDER BY id DESC LIMIT 1", (pid,))
    if row and row["type"] != "analyze":
        # 保留原有写作范围、重写、优化等任务的恢复语义，仅跳过阻挡创作的旧拆书任务。
        stage = {"outline": "outline", "plan": "outline", "write": "write", "polish": "polish",
                 "rewrite": "polish", "context": "check", "check": "check"}.get(row["type"])
        if stage:
            config.resolve_stage(stage)
        db.update_task(row["id"], status="pending", error="", message="继续执行")
        return {"task_id": row["id"], "mode": "resume"}
    outline = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    plan = utils.parse_plan((outline or {}).get("content", ""))
    if plan or (outline and (outline.get("content") or "").strip()):
        cons = db.q1("SELECT locked FROM constitution WHERE project_id=?", (pid,))
        if not cons or not cons["locked"]:
            return {"task_id": None, "mode": "constitution", "message": "已有新书大纲，请确认并锁定创作宪法后开始写作；章节计划会自动整理，无需拆完整本。"}
        written = {r["idx"] for r in db.q("SELECT idx FROM generated WHERE project_id=? AND content!=''", (pid,))}
        if not plan or any(r["idx"] not in written for r in plan):
            config.resolve_stage("write")
            pipeline.finish_analysis(pid)
            tid = db.create_task(pid, "write", "写作：继续下一批（5章）", {"mode": "next"})
            return {"task_id": tid, "mode": "write"}
        return {"task_id": None, "mode": "none", "message": "大纲中现有章节已写完，可去大纲页补充后续计划或导出。"}
    if row:
        if row["type"] == "analyze" and p["meta"].get("analysis_finished"):
            return {"task_id": None, "mode": "outline", "message": "已结束拆书，可用现有内容生成原创大纲。"}
        stage = {"analyze": "analyze", "outline": "outline", "plan": "outline", "write": "write",
                 "polish": "polish", "rewrite": "polish", "context": "check",
                 "check": "check"}.get(row["type"])
        if stage:
            config.resolve_stage(stage)
        db.update_task(row["id"], status="pending", error="", message="继续执行")
        return {"task_id": row["id"], "mode": "resume"}
    if p["meta"].get("analysis_finished"):
        return {"task_id": None, "mode": "outline", "message": "可用已拆内容生成大纲，无需继续拆书。"}
    detail_left = db.q1("SELECT COUNT(*) AS c FROM batches WHERE project_id=? AND kind='detail' AND status!='done'",
                        (pid,))["c"]
    if detail_left:
        config.resolve_stage("analyze")
        tid = db.create_task(pid, "analyze", "拆书：继续下一批", {"mode": "next"})
        return {"task_id": tid, "mode": "analyze"}
    return {"task_id": None, "mode": "none", "message": "当前没有可继续的任务"}


@app.get("/api/health")
async def health():
    busy = db.q1("SELECT COUNT(*) c FROM tasks WHERE status IN ('running','pending')")["c"]
    return {"ok": True, "app": runtime.APP_ID, "root": str(ROOT.resolve()),
            "database": str(db.DB_PATH.resolve()),
            "code_version": runtime.CODE_VERSION, "pid": os.getpid(),
            "busy": bool(busy), "worker_active": tasks._worker_lock is not None, "time": db.now()}


def _port_free(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _open_when_ready(host: str, port: int, url: str) -> None:
    import socket

    target = "127.0.0.1" if host in ("0.0.0.0", "") else host
    for _ in range(120):
        try:
            with socket.create_connection((target, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.5)
    import subprocess
    import sys
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if sys.platform.startswith("win"):
            subprocess.run(["cmd", "/c", "start", "", url], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if webbrowser.open(url):
            return
        subprocess.run(["xdg-open", url], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def main() -> None:
    import uvicorn

    runtime.configure_stdio()
    cfg = config.load()
    host = cfg["server"].get("host", "127.0.0.1")
    preferred = int(cfg["server"].get("port", 8765))
    port = preferred
    if not _port_free(host, port):
        for candidate in range(preferred + 1, preferred + 30):
            if _port_free(host, candidate):
                port = candidate
                break
    url = f"http://{host}:{port}"
    if cfg["server"].get("open_browser", True) and os.environ.get("NOVEL_OPEN_BROWSER") != "0":
        threading.Thread(target=_open_when_ready, args=(host, port, url), daemon=True).start()
    print("", flush=True)
    print("  ────────────────────────────────────────────", flush=True)
    print("  小说智能体已启动", flush=True)
    print(f"  地址： {url}", flush=True)
    print("  浏览器没自动打开的话，复制上面的地址访问。", flush=True)
    print("  关闭这个窗口或按 Ctrl+C 即可停止服务。", flush=True)
    print("  ────────────────────────────────────────────", flush=True)
    if port != preferred:
        print(f"  （{preferred} 端口被占用，已自动改用 {port}）", flush=True)
    print("", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
