"""核心流水线：拆书 → 六维分析 → 原创大纲 → 创作宪法 → 逐章写作 → 承接 → 优化。

每个批次都是一个可断点续跑的任务，所有中间结果落库。
"""
from __future__ import annotations

import json
import math
import re
import asyncio
import hashlib
from collections import Counter

from . import config, db, llm, prompts, utils, zhuque


class Paused(Exception):
    """任务被用户暂停/取消。"""


# ---------------------------------------------------------------- 基础工具

def tick(task_id: int, *, progress: int | None = None, total: int | None = None,
         message: str | None = None) -> None:
    fields: dict = {}
    if progress is not None:
        fields["progress"] = progress
    if total is not None:
        fields["total"] = total
    if message is not None:
        fields["message"] = message
    if fields:
        db.update_task(task_id, **fields)
    if task_id and db.task_status(task_id) in ("paused", "canceled", "missing"):
        raise Paused()


def project(pid: int) -> dict:
    row = db.q1("SELECT * FROM projects WHERE id=?", (pid,))
    if not row:
        raise ValueError(f"项目 {pid} 不存在")
    row["meta"] = json.loads(row.get("meta") or "{}")
    return row


def set_meta(pid: int, patch: dict) -> None:
    row = db.q1("SELECT meta FROM projects WHERE id=?", (pid,))
    meta = json.loads((row or {}).get("meta") or "{}")
    meta.update(patch)
    db.execute("UPDATE projects SET meta=?, updated_at=? WHERE id=?",
               (json.dumps(meta, ensure_ascii=False), db.now(), pid))


def finish_analysis(pid: int) -> None:
    """当前窗口落库后停止拆书；后续创作使用已有结果。"""
    project(pid)
    set_meta(pid, {"analysis_finished": True})
    db.execute("UPDATE tasks SET status='canceled', message='已选择用现有拆书结果创作', updated_at=? "
               "WHERE project_id=? AND type='analyze' AND status IN ('pending','paused','failed')",
               (db.now(), pid))


def add_warning(pid: int, text: str) -> None:
    meta = project(pid)["meta"]
    warnings = meta.get("warnings") or []
    warnings.append({"time": db.now(), "text": text})
    meta["warnings"] = warnings[-20:]
    set_meta(pid, {"warnings": meta["warnings"]})


def reference_title(pid: int) -> str:
    """被拆解的那本高分参考书（别人的书）。"""
    p = project(pid)
    return (p["meta"].get("reference_title") or p["name"] or "参考书").strip()


def new_book_title(pid: int) -> str:
    """要写的新书书名。优先取原创大纲的一级标题，其次取原创要求里的【书名】。"""
    row = db.q1("SELECT content, requirement FROM outline WHERE project_id=?", (pid,))
    if row:
        m = re.search(r"^#\s+(.+)$", row.get("content") or "", re.M)
        if m:
            title = re.sub(r"[*_`#]", "", m.group(1)).strip().strip("《》 ")
            if title:
                return title
        m = re.search(r"【书名】\s*(.+)", row.get("requirement") or "")
        if m:
            title = m.group(1).strip().splitlines()[0].strip("《》 ")
            if title:
                return title
    return "新书"


def check_source_overlap(pid: int, outline_text: str) -> None:
    """粗查大纲有没有照搬参考书的人名/书名，发现就提示，不自动改。"""
    archive_raw = db.get_asset(pid, "archive")
    names: list[str] = []
    ref = reference_title(pid)
    if len(ref) >= 2:
        names.append(ref)
    try:
        archive = json.loads(archive_raw) if archive_raw else {}
        for ch in archive.get("characters", []) or []:
            name = (ch.get("name") or "").strip()
            if name and len(name) >= 2:
                names.append(name)
    except Exception:
        pass
    hits = sorted({n for n in names if n and n in outline_text})
    if hits:
        add_warning(pid, "原创大纲里出现了参考书的人名/书名：" + "、".join(hits[:6]) +
                    "。请改成全新名字，避免照搬。")


async def call_json(stage: str, system: str, user: str, *, pid: int, max_tokens: int,
                    temperature: float, tag: str = "", validator=None, recover=None,
                    max_attempts: int = 3, timeout: float | None = None,
                    model_retries: int | None = None):
    text = ""
    data = None
    valid = False
    last_failure = ""
    # 结构化输出对兼容接口容易受截断和格式差异影响；逐次给出修复提示并增加输出预算。
    for attempt in range(max(1, int(max_attempts))):
        retry_user = user
        if attempt:
            retry_user += (
                "\n\n上一次输出未能解析成 JSON。请重新完成任务，严格只输出一个完整 JSON 对象，"
                "不要 Markdown 代码块或说明文字；检查所有字符串转义和结尾括号。"
            )
        # 拆书分析明确禁用网络截止时间，让慢模型持续返回；其他环节沿用 10 分钟客户端时限。
        chat_timeout = timeout if timeout is not None else (None if stage == "analyze" else 600.0)
        chat_options = {"timeout": chat_timeout}
        if model_retries is not None:
            chat_options["retries"] = model_retries
        request = llm.chat(
            stage, system, retry_user, project_id=pid,
            max_tokens=min(24000, max_tokens * (2 ** attempt)),
            temperature=max(0.1, temperature - attempt * 0.15),
            allow_truncated=recover is not None, **chat_options,
        )
        try:
            text = await asyncio.wait_for(request, timeout=timeout) if timeout is not None else await request
        except asyncio.TimeoutError:
            last_failure = f"模型请求超过 {timeout:g} 秒硬时限" if timeout is not None else "模型请求超时"
            text = ""
            if tag:
                db.upsert_asset(pid, f"{tag}_raw", f"[模型请求超时] {last_failure}")
            continue
        data = utils.extract_json(text)
        if recover is not None and (not isinstance(data, dict) or not data.get("chapters")):
            data = recover(text) or data
        valid = isinstance(data, dict) and bool(data)
        if valid and validator is not None:
            try:
                valid = validator(data)
            except Exception:
                valid = False
        if valid:
            if tag and data.get("partial_output"):
                db.upsert_asset(pid, f"{tag}_raw", text)
            break
        if tag:
            db.upsert_asset(pid, f"{tag}_raw", text)
    if not valid:
        if tag:
            db.upsert_asset(pid, f"{tag}_raw", text)
        reason = "可用的结构化分析内容" if validator else "可用的 JSON 对象"
        suffix = f"；原因：{last_failure}" if last_failure else ""
        raise ValueError(f"{stage} 环节连续 {max(1, int(max_attempts))} 次没有返回{reason}（已保存最后一次原始输出供排查{suffix}）")
    return data, text


# ---------------------------------------------------------------- 项目创建

def create_project(name: str, text: str, filename: str) -> int:
    from .splitter import split_chapters_with_diagnostics

    chapters, split_info = split_chapters_with_diagnostics(text)
    if not chapters:
        raise ValueError("没有解析到任何章节，请检查 TXT 内容。")
    batch_size = int(config.load()["writing"].get("batch_size") or 5)
    pid = db.execute(
        "INSERT INTO projects(name, source_name, source_chars, status, meta, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (name, filename, len(text), "uploaded",
         json.dumps({"batch_size": batch_size, "total_chapters": len(chapters),
                     "reference_title": name}, ensure_ascii=False),
         db.now(), db.now()),
    )
    for i, ch in enumerate(chapters, 1):
        db.execute(
            "INSERT INTO chapters(project_id, idx, title, content, word_count, batch_idx) VALUES(?,?,?,?,?,?)",
            (pid, i, ch["title"], ch["content"], utils.word_count(ch["content"]),
             (i - 1) // batch_size),
        )
    total_batches = math.ceil(len(chapters) / batch_size)
    preflight = build_preflight(text, chapters, batch_size)
    if split_info.get("toc_headings_removed"):
        preflight["warnings"].append(
            f"识别到目录区并跳过 {split_info['toc_headings_removed']} 条目录标题，后续按实际正文分章。"
        )
        preflight["toc_headings_removed"] = split_info["toc_headings_removed"]
    if split_info.get("front_matter_removed"):
        preflight["warnings"].append("识别到书名/作者/简介等前置信息，已从章节正文中排除。")
        preflight["front_matter_removed"] = True
    db.upsert_asset(pid, "preflight", json.dumps(preflight, ensure_ascii=False))
    for b in range(total_batches):
        db.set_batch(pid, "detail", b, "pending")
        db.set_batch(pid, "write", b, "pending")
    return pid


def build_preflight(text: str, chapters: list[dict], batch_size: int) -> dict:
    """上传后立即生成轻量预检，避免用户跑完整本后才发现分章异常。"""
    lengths = [len((ch.get("content") or "").strip()) for ch in chapters]
    titles = [str(ch.get("title") or "").strip() for ch in chapters]
    repeated = sorted(title for title, count in Counter(titles).items() if title and count > 1)
    fallback = bool(chapters) and all(
        re.fullmatch(r"第\d+节", title) for title in titles
    )
    empty = sum(1 for n in lengths if n < 30)
    warnings = []
    if fallback:
        warnings.append("没有识别到标准章节标题，系统已按长度自动分块；建议抽查前几块是否从完整段落开始。")
    if empty:
        warnings.append(f"发现 {empty} 个少于 30 字的空章或短章，拆解结果可能缺少内容。")
    if repeated:
        warnings.append("发现重复章节标题，展示时可能不易区分：" + "、".join(repeated[:8]))
    if len(chapters) < 2:
        warnings.append("只识别到 1 个章节/分块；确认上传内容是否完整。")
    if any(n > 30000 for n in lengths):
        warnings.append("存在超过 3 万字的单章，模型可能需要更多时间；系统会在失败时保留批次并允许续跑。")
    return {
        "source_chars": len(text), "chapter_count": len(chapters),
        "empty_chapters": empty,
        "average_chapter_chars": round(sum(lengths) / len(lengths)) if lengths else 0,
        "shortest_chapter_chars": min(lengths, default=0),
        "longest_chapter_chars": max(lengths, default=0),
        "duplicate_titles": repeated, "fallback_split": fallback,
        "batch_size": batch_size, "batch_count": math.ceil(len(chapters) / batch_size),
        "warnings": warnings,
    }


def quality_report(pid: int) -> dict:
    """由当前数据库状态即时计算，不依赖旧项目迁移或覆盖用户数据。"""
    rows = db.q("SELECT batch_idx, status, error FROM batches WHERE project_id=? AND kind='detail' ORDER BY batch_idx", (pid,))
    details = {}
    for r in db.q("SELECT key, content FROM assets WHERE project_id=? AND key LIKE 'detail_%'", (pid,)):
        suffix = r["key"].split("_")[-1]
        if not r["key"].endswith("_raw") and suffix.isdigit():
            details[int(suffix)] = r
    chapter_count = db.q1("SELECT COUNT(*) AS c FROM chapters WHERE project_id=?", (pid,))["c"]
    covered = 0
    invalid = []
    coverage_gaps = []
    expected_by_batch: dict[int, set[str]] = {}
    for chapter in db.q("SELECT idx, batch_idx FROM chapters WHERE project_id=?", (pid,)):
        expected_by_batch.setdefault(int(chapter["batch_idx"]), set()).add(str(chapter["idx"]))
    for b, row in details.items():
        try:
            data = json.loads(row["content"])
            chapters = data.get("chapters") or []
            expected_idxs = expected_by_batch.get(b, set())
            usable = [c for c in chapters if isinstance(c, dict) and (c.get("summary") or "").strip()]
            # 模型章节编号可能与原文解析结果不同；覆盖率按有效分析条目数估算，不因此判失败。
            covered += min(len(usable), len(expected_idxs))
            if len(usable) < len(expected_idxs):
                coverage_gaps.append(b)
        except Exception:
            invalid.append(b)
    failed = [r["batch_idx"] for r in rows if r["status"] in ("failed", "archive_failed")]
    pending = [r["batch_idx"] for r in rows if r["status"] not in ("done",)]
    return {
        "total_batches": len(rows), "completed_batches": sum(r["status"] == "done" for r in rows),
        "failed_batches": failed, "pending_batches": pending,
        "chapter_count": chapter_count, "covered_chapters": covered,
        "coverage_percent": round(covered * 100 / chapter_count) if chapter_count else 0,
        "invalid_detail_batches": invalid,
        "coverage_gap_batches": coverage_gaps,
        "archive_ready": bool(db.get_asset(pid, "archive")),
        "modules_ready": bool(db.get_asset(pid, "modules")),
        "raw_output_keys": [r["key"] for r in db.q("SELECT key FROM assets WHERE project_id=? AND key LIKE '%_raw' ORDER BY key", (pid,))],
        "retry_batches": sorted(set(failed)),
    }


def chapters_in_batch(pid: int, batch_idx: int, batch_size: int) -> list[dict]:
    return db.q("SELECT * FROM chapters WHERE project_id=? AND batch_idx=? ORDER BY idx",
                (pid, batch_idx))


def _build_framework_detail(chapters: list[dict], batch_idx: int, reason: str) -> dict:
    """拆解模型失败或来源只有标题时，保存不臆造剧情的最小章节框架。"""
    rows = []
    for chapter in chapters:
        title = str(chapter.get("title") or "未命名章节").strip()
        body = str(chapter.get("content") or "").strip()
        if body:
            excerpt = body if len(body) <= 420 else body[:300] + "……" + body[-100:]
            summary = f"模型拆解未成功，先保留原文线索：{excerpt}"
        else:
            summary = f"正文缺失，仅保留章节《{title}》的位置作为框架，不推断具体剧情。"
        rows.append({
            "idx": chapter["idx"], "title": title, "summary": summary,
            "characters": "", "conflict": "", "hook": "", "hook_type": "",
        })
    reason = str(reason or "未知原因")[:500]
    return {
        "chapters": rows,
        "batch_notes": {
            "mainline_progress": "本批使用简框架保留章节位置；不把缺失内容臆测为已发生剧情。",
            "worldbuilding": "待有可用正文后补充。",
            "pacing": "待有可用正文后补充。",
            "error_reason": reason,
        },
        "takeaways": [], "batch_index": batch_idx,
        "analysis_mode": "framework", "partial_output": True,
        "error_reason": reason,
    }


def detail_is_complete(pid: int, batch_idx: int) -> bool:
    raw = db.get_asset(pid, f"detail_{batch_idx}")
    if not raw:
        return False
    try:
        data = json.loads(raw)
        # 允许模型返回的章节编号、数量与原文不完全一致；有摘要即可复用。
        usable = [ch for ch in (data.get("chapters") or [])
                  if isinstance(ch, dict) and (ch.get("summary") or "").strip()]
        return bool(usable)
    except Exception:
        return False


def detail_batch_count(pid: int, batch_size: int) -> int:
    n = db.q1("SELECT COUNT(*) AS c FROM chapters WHERE project_id=?", (pid,))["c"]
    return math.ceil(n / batch_size) if n else 0


# ---------------------------------------------------------------- 拆书

def _detail_digest(pid: int, max_batches: int = 120) -> str:
    rows = db.q("SELECT key, content FROM assets WHERE project_id=? AND key LIKE 'detail_%'", (pid,))
    items = []
    for r in rows:
        key = r["key"]
        if "_raw" in key:
            continue
        try:
            b = int(key.split("_")[1])
        except Exception:
            continue
        items.append((b, r["content"]))
    items.sort()
    max_batches = max(2, max_batches)
    if len(items) > max_batches:
        # 均匀抽样，保证开头、中段、结尾都进入大纲参考
        indexes = [round(i * (len(items) - 1) / (max_batches - 1)) for i in range(max_batches)]
        items = [items[i] for i in indexes]
    parts: list[str] = []
    for b, content in items:
        try:
            data = json.loads(content)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        lines = [f"【第{b + 1}批】"]
        notes = data.get("batch_notes") or {}
        if not isinstance(notes, dict):
            notes = {}
        if notes.get("mainline_progress"):
            lines.append(f"- 主线推进：{str(notes['mainline_progress'])[:240]}")
        for ch in data.get("chapters", []):
            if not isinstance(ch, dict) or not ch.get("summary"):
                continue
            hook = str(ch.get("hook") or "").replace("\n", " ")[:60]
            lines.append(f"- {ch.get('title', '')}：{str(ch.get('summary') or '')[:220]}｜钩子：{hook}")
        if notes.get("pacing"):
            lines.append(f"- 节奏：{str(notes['pacing'])[:120]}")
        for t in data.get("takeaways", [])[:3]:
            lines.append(f"- 可借鉴：{str(t)[:100]}")
        if len(lines) > 1:
            parts.append("\n".join(lines))
    # 分摊预算，避免整段从头截断后丢掉已拆范围的中后段。
    per_batch = max(100, 27000 // max(len(parts), 1))
    return "\n\n".join(utils.clip(part, per_batch) for part in parts)


async def analyze_batch(task_id: int, pid: int, b: int, batch_size: int, book: str) -> None:
    """分析一个批次并保存细纲；全书档案在外层按批次顺序更新。"""
    chapters = chapters_in_batch(pid, b, batch_size)
    if not chapters:
        db.set_batch(pid, "detail", b, "done")
        return
    if not any((chapter.get("content") or "").strip() for chapter in chapters):
        data = _build_framework_detail(chapters, b, "原文只有章节标题，没有可供拆解的正文")
        db.upsert_asset(pid, f"detail_{b}", json.dumps(data, ensure_ascii=False))
        db.execute("DELETE FROM assets WHERE project_id=? AND key='modules'", (pid,))
        return
    start, end = chapters[0]["idx"], chapters[-1]["idx"]
    raw = "\n\n".join(f"### {c['title']}\n{c['content']}" for c in chapters)
    raw = utils.clip(raw, 30000)
    system, user = prompts.detail_outline(book, start, end, raw, len(chapters_meta(pid)))
    def complete_detail(data: dict) -> bool:
        rows = data.get("chapters") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return False
        # 章节对不上时仍继续拆解。只要求返回至少一条有实际内容的分析，
        # 覆盖率和缺失情况交给质量报告展示，不阻断主流程。
        return any(isinstance(ch, dict) and (ch.get("summary") or "").strip() for ch in rows)

    def fallback_detail(reason: str) -> dict:
        """模型无法结构化输出时保留章节骨架，让后续批次继续处理。"""
        data = _build_framework_detail(chapters, b, reason)
        return data

    # 旧失败批次可能已有完整摘要，优先回收，避免重新消耗模型额度。
    data = None
    if not detail_is_complete(pid, b):
        data = utils.recover_detail(db.get_asset(pid, f"detail_{b}_raw"))
        # 旧失败回复可能是合法 JSON 但 chapters 为空；不能把空壳当成可用细纲。
        if data and not complete_detail(data):
            data = None
    if not data:
        # 标题存在但正文为空时无需浪费模型调用，直接生成最小可用框架。
        if not any((c.get("content") or "").strip() for c in chapters):
            data = fallback_detail("原文只有章节标题，没有可供拆解的正文")
            add_warning(pid, f"第 {b + 1} 批仅有章节标题，已保存框架并继续拆后续批次。")
        else:
            try:
                data, _ = await call_json("analyze", system, user, pid=pid, max_tokens=3000,
                                          temperature=0.3, tag=f"detail_{b}",
                                          validator=complete_detail,
                                          recover=utils.recover_detail, max_attempts=1)
            except (Paused, asyncio.CancelledError):
                raise
            except Exception as exc:  # noqa: BLE001
                data = fallback_detail(f"结构化拆解失败：{exc}")
                add_warning(pid, f"第 {b + 1} 批拆解失败，已保存最小框架并继续后续批次：{exc}")
    data["chapters"] = [ch for ch in data["chapters"] if isinstance(ch, dict)
                        and isinstance(ch.get("summary"), str) and ch["summary"].strip()]
    if not isinstance(data.get("batch_notes"), dict):
        data["batch_notes"] = {}
    if not isinstance(data.get("takeaways"), list):
        data["takeaways"] = []
    data["batch_index"] = b
    db.upsert_asset(pid, f"detail_{b}", json.dumps(data, ensure_ascii=False))
    # 细纲变了，旧的六维分析作废，等全部拆完后重新汇总
    db.execute("DELETE FROM assets WHERE project_id=? AND key='modules'", (pid,))

async def update_archive_batches(pid: int, batch_indexes: list[int], book: str) -> None:
    """串行合并已完成批次，避免并发写入互相覆盖全书档案。"""
    if not batch_indexes:
        return
    details = []
    for b in batch_indexes:
        detail_raw = db.get_asset(pid, f"detail_{b}")
        if not detail_raw:
            raise ValueError(f"第 {b + 1} 批没有可合并的细纲")
        source = json.loads(detail_raw)
        # 档案只需稳定吸收事实和人物变化，不需要再次发送整份细纲，降低 token 与截断风险。
        chapters = []
        for ch in source.get("chapters", []):
            if not isinstance(ch, dict):
                continue
            chapters.append({key: str(ch.get(key) or "")[:limit] for key, limit in (
                ("idx", 20), ("title", 100), ("summary", 420), ("characters", 500),
                ("conflict", 240), ("hook", 180), ("hook_type", 40))})
        notes = source.get("batch_notes") or {}
        details.append({
            "batch_index": b,
            "chapters": chapters,
            "batch_notes": {key: str(notes.get(key) or "")[:220] for key in
                            ("mainline_progress", "worldbuilding", "patterns", "pacing")},
            "takeaways": [str(item)[:160] for item in (source.get("takeaways") or [])[:3]],
        })
    old = db.get_asset(pid, "archive")
    try:
        s2, u2 = prompts.archive_update(book, old, json.dumps(details, ensure_ascii=False))
        arch, _ = await call_json("analyze", s2, u2, pid=pid, max_tokens=4200, temperature=0.2,
                                  tag="archive")
        db.upsert_asset(pid, "archive", json.dumps(arch, ensure_ascii=False))
    except llm.LLMError:
        raise
    except Paused:
        raise
    except Exception:
        raise


def chapters_meta(pid: int) -> list[dict]:
    return db.q("SELECT idx, title FROM chapters WHERE project_id=? ORDER BY idx", (pid,))


async def run_modules(pid: int, book: str) -> None:
    archive = db.get_asset(pid, "archive")
    modules = await call_json_modules(pid, book, archive)
    if modules:
        db.upsert_asset(pid, "modules", json.dumps(modules, ensure_ascii=False))


async def call_json_modules(pid: int, book: str, archive: str):
    digests = _detail_digest(pid)
    system, user = prompts.six_dimensions(book, archive, digests)
    try:
        data, _ = await call_json("analyze", system, user, pid=pid, max_tokens=5000,
                                  temperature=0.4, tag="modules")
        return data
    except Exception as exc:
        add_warning(pid, f"六维分析失败：{exc}")
        return None


async def run_analysis(task_id: int, pid: int, payload: dict) -> None:
    p = project(pid)
    book = reference_title(pid)  # 拆的是参考书，不是新书
    batch_size = int(p["meta"].get("batch_size") or 5)
    total = detail_batch_count(pid, batch_size)
    mode = payload.get("mode", "next")
    forced_batch = payload.get("batch")

    if forced_batch is not None:
        targets = [int(forced_batch)]
    elif mode == "all":
        rows = db.q("SELECT batch_idx FROM batches WHERE project_id=? AND kind='detail' "
                    "AND status!='done' ORDER BY batch_idx", (pid,))
        targets = [r["batch_idx"] for r in rows]
    else:
        row = db.q1("SELECT batch_idx FROM batches WHERE project_id=? AND kind='detail' "
                    "AND status!='done' ORDER BY batch_idx LIMIT 1", (pid,))
        targets = [row["batch_idx"]] if row else []

    if not targets:
        tick(task_id, progress=1, total=1, message="所有细纲都已拆完")
    failed = 0
    last_err = ""
    framework_batches = 0
    framework_total = 0
    # 严格逐批处理：当前批正文和档案全部落库后，再启动下一批。
    analysis_cfg = config.load().get("analysis", {})
    parallel = 1
    archive_interval = max(1, min(5, int(analysis_cfg.get("archive_interval") or 2)))
    processed = 0
    for offset in range(0, len(targets), parallel):
        if project(pid)["meta"].get("analysis_finished"):
            break
        window = targets[offset:offset + parallel]
        window_results = {}
        for b in window:
            tick(task_id, message=f"正在拆第 {b + 1} 批（第 {b * batch_size + 1}-{(b + 1) * batch_size} 章）")
            db.set_batch(pid, "detail", b, "running")

        async def run_detail(b: int):
            try:
                # 自动续跑时保留已有完整细纲，只重试失败的档案合并；用户点击“拆这批”则按要求重新分析。
                chapters = chapters_in_batch(pid, b, batch_size)
                has_source_text = any((chapter.get("content") or "").strip() for chapter in chapters)
                if not has_source_text:
                    # 旧版可能按标题编出了长摘要；来源无正文时统一降为清楚标注的章节框架。
                    await analyze_batch(task_id, pid, b, batch_size, book)
                    return b, None
                existing_detail = {}
                try:
                    existing_detail = json.loads(db.get_asset(pid, f"detail_{b}"))
                except (TypeError, ValueError):
                    pass
                reusable_analysis = (detail_is_complete(pid, b)
                                     and existing_detail.get("analysis_mode") != "framework")
                if not (forced_batch is not None) and reusable_analysis:
                    return b, None
                await analyze_batch(task_id, pid, b, batch_size, book)
                return b, None
            except Exception as exc:  # noqa: BLE001
                if isinstance(exc, Paused):
                    return b, exc
                # 单批模型/API故障降级成章节框架；批次失败不再阻断后续队列。
                chapters = chapters_in_batch(pid, b, batch_size)
                if not chapters:
                    return b, exc
                reason = f"自动降级为章节框架：{exc}"
                data = _build_framework_detail(chapters, b, reason)
                db.upsert_asset(pid, f"detail_{b}", json.dumps(data, ensure_ascii=False))
                db.execute("DELETE FROM assets WHERE project_id=? AND key='modules'", (pid,))
                add_warning(pid, f"第 {b + 1} 批拆解失败，已改用章节框架并继续后续批次：{exc}")
                return b, None

        results = []
        for b in window:
            results.append(await run_detail(b))
        for b, error in results:
            window_results[b] = error
            if isinstance(error, Paused):
                db.set_batch(pid, "detail", b, "paused")
                raise error
            if error:
                if isinstance(error, llm.LLMError):
                    db.set_batch(pid, "detail", b, "failed", str(error)[:300])
                else:
                    db.set_batch(pid, "detail", b, "failed", str(error)[:300])
                failed += 1
                last_err = str(error)[:200]
                add_warning(pid, f"第 {b + 1} 批拆书失败：{error}")

        # 某批失败不再阻塞后面的批次；档案按当前窗口中能用的细纲继续增量合并。
        successful = []
        for b in sorted(window):
            if window_results.get(b):
                continue
            successful.append(b)
        for b in successful:
            try:
                data = json.loads(db.get_asset(pid, f"detail_{b}"))
            except (TypeError, ValueError):
                data = {}
            if data.get("analysis_mode") == "framework":
                framework_batches += 1
                reason = str(data.get("error_reason") or "本批正文不足，已保留章节框架")[:300]
                db.set_batch(pid, "detail", b, "done", reason)
        for start_at in range(0, len(successful), archive_interval):
            group = successful[start_at:start_at + archive_interval]
            # 纯标题框架不交给档案模型推断人物或剧情，跳过无意义调用。
            archive_group = []
            for b in group:
                try:
                    data = json.loads(db.get_asset(pid, f"detail_{b}"))
                except (TypeError, ValueError):
                    data = {}
                if data.get("analysis_mode") != "framework":
                    archive_group.append(b)
            if not archive_group:
                continue
            try:
                await update_archive_batches(pid, archive_group, book)
                for b in archive_group:
                    reason = ""
                    try:
                        detail = json.loads(db.get_asset(pid, f"detail_{b}"))
                        reason = str(detail.get("error_reason") or "")[:300]
                    except (TypeError, ValueError):
                        pass
                    db.set_batch(pid, "detail", b, "done", reason)
            except Paused:
                for b in group:
                    db.set_batch(pid, "detail", b, "paused")
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:200]
                # 细纲已落库；档案合并失败只记原因，不把批次重新判失败，也不阻塞后续。
                for b in archive_group:
                    db.set_batch(pid, "detail", b, "done", f"档案合并失败，细纲已保留：{str(exc)[:220]}")
                add_warning(pid, f"第 {archive_group[0] + 1}-{archive_group[-1] + 1} 批档案合并失败，继续拆后续批次：{exc}")
                # 当前档案合并失败只影响这一小组，后面的细纲继续处理。
        processed += len(window)
        tick(task_id, progress=processed, total=len(targets),
             message=f"已处理 {processed}/{len(targets)} 批")
    # 全部拆完 → 六维分析
    left = db.q1("SELECT COUNT(*) AS c FROM batches WHERE project_id=? AND kind='detail' AND status!='done'",
                 (pid,))["c"]
    if failed or left:
        add_warning(pid, f"自动拆书已继续处理后续批次；仍有 {left} 批需要补处理，另有 {failed} 批因数据缺失无法生成框架。请查看批次原因。")
    has_modules = bool(db.get_asset(pid, "modules"))
    if left == 0 and not has_modules and not project(pid)["meta"].get("analysis_finished"):
        all_details = db.q("SELECT key,content FROM assets WHERE project_id=? AND key LIKE 'detail_%'", (pid,))
        for item in all_details:
            if not re.fullmatch(r"detail_\d+", item["key"]):
                continue
            try:
                framework_total += int(json.loads(item["content"]).get("analysis_mode") == "framework")
            except (TypeError, ValueError):
                continue
        if total and framework_total >= max(1, math.ceil(total / 2)):
            add_warning(pid, f"本书 {framework_total}/{total} 批只有标题框架，已跳过信息不足的六维分析；可直接用现有框架生成大纲。")
            tick(task_id, message=f"正文不足，已保留 {framework_total} 批章节框架并跳过六维分析")
        else:
            tick(task_id, message="细纲完成，正在汇总六维分析")
            await run_modules(pid, book)
        db.execute("UPDATE projects SET status='analyzed', updated_at=? WHERE id=?", (db.now(), pid))
    stopped = project(pid)["meta"].get("analysis_finished")
    final_message = "已在本批结束拆书，可用现有内容生成大纲" if stopped else "拆书任务完成"
    if failed or left:
        final_message += f"；有 {left} 批保留失败原因，不影响其他批次继续"
    if framework_batches or framework_total:
        framework_batches = max(framework_batches, framework_total)
        final_message += f"；其中 {framework_batches} 批仅保留框架，失败原因见对应批次"
    tick(task_id, progress=processed, total=max(len(targets), 1), message=final_message)


# ---------------------------------------------------------------- 原创大纲

def save_chapter_plan(pid: int, content: str, rows: list[dict]) -> str:
    normalized = utils.attach_plan(content, rows)
    if normalized != content:
        if not db.get_asset(pid, "outline_before_plan_repair"):
            db.upsert_asset(pid, "outline_before_plan_repair", content)
        db.execute("UPDATE outline SET content=?, updated_at=? WHERE project_id=?",
                   (normalized, db.now(), pid))
    return normalized


async def prepare_chapter_plan(pid: int, task_id: int = 0) -> list[dict]:
    """只依赖新书大纲；无机器格式时自动提取，不要求参考书全部拆完。"""
    row = db.q1("SELECT content,requirement FROM outline WHERE project_id=?", (pid,))
    if not row or not row["content"].strip():
        raise ValueError("还没有新书大纲，请先用已有拆书结果生成大纲，无需拆完整本。")
    content = row["content"]
    rows = utils.parse_plan(content)
    if not rows:
        tick(task_id, message="正在从现有大纲提取可写的章节计划（无需继续拆书）")
        system = prompts.SYSTEM_EDITOR
        user = (
            "请把以下新书大纲整理为可执行的章节计划。保持书名、人物、主线、因果和世界规则，"
            "只分解和细化这本新书，不照搬参考书，不需要任何额外参考书章节。"
            "若大纲只有分卷描述，按用户要求分成章节；若有逐章内容，优先保留已有顺序。"
            "只输出一个 JSON 对象，chapters 数组每项包含 idx（正整数）、title、event（本章核心事件）、payoff、hook。"
            "event 简洁具体，先保证核心事件可用于写作，不要解释或输出思考过程。\n"
            f"用户要求：{row.get('requirement') or '按大纲自然展开'}\n\n新书大纲：\n{content}"
        )
        def usable(data):
            return isinstance(data.get("chapters"), list) and any(
                isinstance(ch, dict) and isinstance(ch.get("event"), str) and ch["event"].strip()
                for ch in data["chapters"])
        data, _ = await call_json("outline", system, user, pid=pid, max_tokens=8000,
                                  temperature=0.3, tag="chapter_plan", validator=usable)
        rows = []
        for i, ch in enumerate(data["chapters"], 1):
            if not isinstance(ch, dict) or not isinstance(ch.get("event"), str) or not ch["event"].strip():
                continue
            idx = utils.cn_to_int(str(ch.get("idx") or i)) or i
            rows.append({"idx": idx, "title": str(ch.get("title") or f"第{idx}章"),
                         "event": ch["event"], "payoff": str(ch.get("payoff") or ""),
                         "hook": str(ch.get("hook") or "")})
        rows = sorted({r["idx"]: r for r in rows}.values(), key=lambda r: r["idx"])
    if not rows:
        raise ValueError("本次未提取到可写计划，原大纲已保留，可点「补提取章节计划」重试。")
    fresh = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    if not fresh or fresh["content"] != content:
        raise ValueError("提取期间大纲已更新，请对最新大纲重新提取，避免覆盖编辑内容。")
    save_chapter_plan(pid, content, rows)
    return rows


async def run_plan(task_id: int, pid: int, payload: dict) -> None:
    rows = await prepare_chapter_plan(pid, task_id)
    tick(task_id, progress=1, total=1, message=f"已保存 {len(rows)} 条章节计划，可以按大纲开始写作")


async def run_outline(task_id: int, pid: int, payload: dict) -> None:
    p = project(pid)
    book = reference_title(pid)  # 只学参考书的手法与结构
    requirement = (payload.get("requirement") or "").strip()
    tick(task_id, progress=0, total=3, message="正在生成原创大纲")
    archive = db.get_asset(pid, "archive")
    modules_raw = db.get_asset(pid, "modules")
    digests = _detail_digest(pid)
    if not archive and not digests:
        raise ValueError("还没有可用的拆书结果，请先拆至少一批；无需拆完整本。")
    archive = archive or "（档案尚未生成，请直接以已有细纲中的主线、人物目标和关键转折为参考。）"
    modules_text = modules_raw or "（六维分析尚未生成，请基于档案和细纲自行推断）"
    source_scope = ("\n【参考范围】以下只代表已经拆解的部分，不一定覆盖全书；"
                    "未拆章节不要推测为参考书事实。档案可能尚未合并最新批次，请同时参考后面的细纲。"
                    "新书主线与结局按用户要求独立创作。\n")
    digests = source_scope + digests
    system, user = prompts.original_outline(book, requirement, archive, modules_text, digests)
    content = await llm.chat("outline", system, user, project_id=pid,
                             max_tokens=8000, temperature=0.9)
    content = re.sub(r"^```(?:markdown)?\s*", "", content.strip())
    content = re.sub(r"\s*```$", "", content)
    check_source_overlap(pid, content)
    db.execute(
        """INSERT INTO outline(project_id, requirement, content, status, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(project_id) DO UPDATE SET requirement=excluded.requirement,
             content=excluded.content, status='draft', updated_at=excluded.updated_at""",
        (pid, requirement, content, "draft", db.now()),
    )
    plan = await prepare_chapter_plan(pid, task_id)
    content = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))["content"]
    tick(task_id, progress=1, message="大纲已生成，正在提炼创作宪法")

    system, user = prompts.constitution(content, archive)
    cons = await llm.chat("outline", system, user, project_id=pid, max_tokens=4500, temperature=0.5)
    db.execute(
        """INSERT INTO constitution(project_id, content, locked, updated_at) VALUES(?,?,0,?)
           ON CONFLICT(project_id) DO UPDATE SET content=excluded.content, locked=0,
             updated_at=excluded.updated_at""",
        (pid, cons, db.now()),
    )
    db.execute("UPDATE projects SET status='outlined', updated_at=? WHERE id=?", (db.now(), pid))
    tick(task_id, progress=3, total=3,
         message=f"大纲完成，共 {len(plan)} 章计划；请到创作宪法页确认并锁定后再开写")


async def regen_constitution(pid: int) -> str:
    row = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    if not row or not row["content"]:
        raise ValueError("还没有大纲。")
    system, user = prompts.constitution(row["content"], db.get_asset(pid, "archive"))
    cons = await llm.chat("outline", system, user, project_id=pid, max_tokens=4500, temperature=0.5)
    db.execute(
        """INSERT INTO constitution(project_id, content, locked, updated_at) VALUES(?,?,0,?)
           ON CONFLICT(project_id) DO UPDATE SET content=excluded.content, locked=0,
             updated_at=excluded.updated_at""",
        (pid, cons, db.now()),
    )
    return cons


# ---------------------------------------------------------------- 写作

def _get_constitution(pid: int) -> dict:
    row = db.q1("SELECT * FROM constitution WHERE project_id=?", (pid,))
    if not row or not row["content"].strip():
        raise ValueError("还没有创作宪法，请先在大纲页生成。")
    if not row["locked"]:
        raise ValueError("创作宪法还没有锁定。请先确认人物、主题、文风、爽点，勾选锁定后再开写。")
    return row


def _plan_groups(plan: list[dict], size: int) -> list[list[dict]]:
    return [plan[i:i + size] for i in range(0, len(plan), size)]


def _gen_row(pid: int, idx: int) -> dict | None:
    return db.q1("SELECT * FROM generated WHERE project_id=? AND idx=?", (pid, idx))


def _batch_complete(pid: int, group: list[dict]) -> bool:
    for r in group:
        row = _gen_row(pid, r["idx"])
        if not row or not (row.get("content") or "").strip():
            return False
    return True


def _last_context(pid: int, b: int) -> dict:
    if b <= 0:
        return {}
    row = db.q1("SELECT content FROM contexts WHERE project_id=? AND after_batch=?", (pid, b - 1))
    if not row or not row["content"]:
        return {}
    try:
        return json.loads(row["content"])
    except Exception:
        return {"context_card": row["content"]}


def _context_text(ctx: dict) -> str:
    if not ctx:
        return ""
    parts = [ctx.get("context_card") or ""]
    states = ctx.get("character_states") or []
    if states:
        parts.append("\n人物状态：")
        for s in states:
            parts.append(f"- {s.get('name', '')}：{s.get('state', '')}；目标：{s.get('goal', '')}；关系：{s.get('relationship', '')}")
    threads = ctx.get("threads") or []
    if threads:
        parts.append("\n未回收线索：")
        for t in threads:
            parts.append(f"- {t.get('item', '')}（{t.get('status', '')}）")
    if ctx.get("next_hook"):
        parts.append(f"\n下一批要接住的钩子：{ctx['next_hook']}")
    deviations = ctx.get("deviations") or []
    if deviations:
        parts.append("\n上一批已确认的偏离与拉回要求（下一批必须执行）：")
        for d in deviations:
            chapter = d.get("chapter") or d.get("chapter_idx") or "本批"
            evidence = f"；证据：{d.get('evidence')}" if d.get("evidence") else ""
            parts.append(f"- 第{chapter}章（{d.get('level', '待定')}）：{d.get('issue', '')}{evidence}；拉回：{d.get('suggestion', '')}")
    return "\n".join(parts)


def _states_text(ctx: dict) -> str:
    states = (ctx or {}).get("character_states") or []
    return "\n".join(f"- {s.get('name', '')}：{s.get('state', '')}；目标：{s.get('goal', '')}"
                     for s in states)


def _prev_chapter(pid: int, idx: int) -> dict | None:
    return db.q1("SELECT * FROM generated WHERE project_id=? AND idx<? ORDER BY idx DESC LIMIT 1",
                 (pid, idx))


def _chapter_text(row: dict, polished: bool = True) -> str:
    if polished and (row.get("polished") or "").strip():
        return row["polished"]
    return row.get("content") or ""


def _chapter_passed(row: dict | None, cfg: dict | None = None) -> bool:
    """已完成检测且分数不高于 60，才可继续写下一章。"""
    if not row or row.get("status") not in {"checked", "medium_aigc", "high_aigc"}:
        return False
    try:
        score = float(row.get("zhuque_score"))
    except (TypeError, ValueError):
        return False
    return score <= 60


def _zhuque_status(score: float) -> str:
    """人工率高于 50% 标绿；疑似 AI 不超过 60% 可继续；高于 60% 限制写作。"""
    if score < 50:
        return "checked"
    if score <= 60:
        return "medium_aigc"
    return "high_aigc"


def _flagged_ranges(segments: list | None, text_len: int) -> list[tuple[int, int]]:
    """把相邻的 label=2 分段合并成可改写的字符区间。"""
    ranges: list[list[int]] = []
    for item in segments or []:
        if not isinstance(item, dict) or int(item.get("label", 0)) != 2:
            continue
        position = item.get("position") or []
        if not isinstance(position, (list, tuple)) or len(position) < 2:
            continue
        try:
            start, length = int(position[0]), int(position[1])
        except (TypeError, ValueError):
            continue
        start = max(0, start)
        end = min(text_len, start + max(0, length))
        if start >= end:
            continue
        if ranges and start <= ranges[-1][1] + 80:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    return [(a, b) for a, b in ranges if b - a >= 40]


def _clean_repair_text(text: str, max_len: int) -> str:
    """只保留模型返回的段落正文，拒绝整章标题、解释和超长输出。"""
    value = (text or "").strip()
    value = re.sub(r"^```(?:markdown|text)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value)
    value = re.sub(r"^(?:改写后|修改后|新段落|替换段)[：:]\s*", "", value.strip())
    value = value.strip()
    if not value or len(value) > max_len:
        return ""
    if utils.CHAPTER_HEAD_RE.search(value):
        return ""
    return value


async def _repair_flagged_segments(task_id: int, pid: int, idx: int, text: str,
                                   score: float, res: dict, target: float,
                                   rounds: int) -> tuple[str, float, dict, bool]:
    """按朱雀分段定位高风险段，只压缩这些段，保留其余正文和剧情事实。"""
    best_text, best_score, best_res = text, float(score), res
    repaired = False
    try:
        constitution_text = _get_constitution(pid)["content"]
    except ValueError as exc:
        add_warning(pid, f"第 {idx} 章自动压缩跳过：{exc}")
        return best_text, best_score, best_res, repaired
    book = new_book_title(pid)
    tried: set[tuple[int, int]] = set()
    for round_no in range(max(0, int(rounds))):
        if best_score < target:
            break
        segments = (best_res.get("diagnostics") or {}).get("segments") or []
        ranges = [item for item in _flagged_ranges(segments, len(best_text)) if item not in tried]
        if not ranges:
            break
        start, end = max(ranges, key=lambda item: item[1] - item[0])
        original_len = end - start
        if original_len < 60:
            break
        segment = best_text[start:end]
        before_ctx = best_text[max(0, start - 260):start]
        after_ctx = best_text[end:end + 260]
        tick(task_id, message=f"第 {idx} 章朱雀 {best_score}，正在压缩第 {round_no + 1} 处高风险段落")
        system, user = prompts.repair_segment(book, constitution_text, segment, before_ctx, after_ctx)
        try:
            raw = await llm.chat("polish", system, user, project_id=pid,
                                 max_tokens=min(6000, max(1200, int(original_len * 1.2))),
                                 temperature=0.75, timeout=180)
        except Paused:
            raise
        except Exception as exc:  # noqa: BLE001
            add_warning(pid, f"第 {idx} 章高风险段改写失败：{exc}")
            break
        replacement = _clean_repair_text(raw, int(original_len * 1.05))
        if len(replacement) < max(60, int(original_len * 0.25)):
            add_warning(pid, f"第 {idx} 章高风险段改写过短或格式不对，换下一段重试。")
            tried.add((start, end))
            continue
        candidate = best_text[:start] + replacement + best_text[end:]
        try:
            res2 = await zhuque.detect(candidate)
        except Exception as exc:  # noqa: BLE001
            add_warning(pid, f"第 {idx} 章高风险段改写后检测失败：{exc}")
            break
        score2 = res2.get("score")
        if not res2.get("ok") or score2 is None:
            add_warning(pid, f"第 {idx} 章高风险段改写后检测未成功：{res2.get('error', '')}")
            break
        if float(score2) >= best_score:
            add_warning(pid, f"第 {idx} 章高风险段改写未降分（{best_score} → {score2}），换下一段重试。")
            tried.add((start, end))
            continue
        best_text, best_score, best_res = candidate, float(score2), res2
        repaired = True
        db.upsert_asset(pid, f"zhuque_{idx}_{task_id}_repair_{round_no}",
                        json.dumps({"round": round_no + 1, "score": score2,
                                    "ratio": (res2.get("diagnostics") or {}).get("ratio_confidence"),
                                    "start": start, "end": end,
                                    "replaced_chars": len(replacement),
                                    "segments": (res2.get("diagnostics") or {}).get("segments")},
                                   ensure_ascii=False))
    return best_text, best_score, best_res, repaired


async def _auto_zhuque_check(task_id: int, pid: int, idx: int) -> None:
    """正文生成后只调用一次朱雀；余额不足时转到网页免费额度。"""
    cfg = config.load().get("zhuque", {})
    if not cfg.get("enabled") or cfg.get("mode") == "manual" or cfg.get("auto_detect", True) is False:
        row = _gen_row(pid, idx)
        if not row:
            return
        text = _chapter_text(row)
        diagnostic = {"chapter": idx, "round": 1, "checked_at": db.now(),
                      "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                      "text_chars": len(text), "applied": False, "source": "content",
                      "adapter": cfg.get("adapter", "generic"), "ok": False, "score": None,
                      "balance": None, "manual_fallback": True,
                      "labels_ratio": {},
                      "error": "API 自动检测未启用，请复制正文到朱雀网页检测并记录分数。"}
        previous_score = row.get("zhuque_score")
        previous_status = row.get("status")
        if previous_score is not None and previous_status in {"checked", "medium_aigc", "high_aigc"}:
            human = round(max(0.0, 100 - float(previous_score)), 1)
            diagnostic.update({"score": previous_score, "ok": True, "manual": True,
                               "source": "retained", "human_rate": human,
                               "error": "API 自动检测未启用，保留上次分数。"})
            db.upsert_asset(pid, f"zhuque_{idx}_{task_id}_0", json.dumps(diagnostic, ensure_ascii=False))
            db.upsert_asset(pid, f"zhuque_{idx}_latest", json.dumps(diagnostic, ensure_ascii=False))
            db.execute("UPDATE generated SET status=?, updated_at=? WHERE project_id=? AND idx=?",
                       (_zhuque_status(float(previous_score)), db.now(), pid, idx))
            add_warning(pid, f"第 {idx} 章未调用朱雀 API，保留上次分数 {previous_score}（人工率 {human}%）。")
            return
        db.upsert_asset(pid, f"zhuque_{idx}_{task_id}_0", json.dumps(diagnostic, ensure_ascii=False))
        db.upsert_asset(pid, f"zhuque_{idx}_latest", json.dumps(diagnostic, ensure_ascii=False))
        db.execute("UPDATE generated SET status='await_check', updated_at=? WHERE project_id=? AND idx=?",
                   (db.now(), pid, idx))
        return
    row = _gen_row(pid, idx)
    if not row:
        return
    text = _chapter_text(row)
    tick(task_id, message=f"第 {idx} 章朱雀检测（自动检测 1 次）")
    res = await zhuque.detect(text)
    _ensure_unchanged(pid, idx, row)
    score = res.get("score")
    balance = res.get("balance")
    threshold = 60
    try:
        low_balance = balance is not None and float(balance) < float(cfg.get("balance_minimum", 1))
    except (TypeError, ValueError):
        low_balance = False
    switch_to_manual = bool(res.get("manual_fallback")) or low_balance
    manual_fallback = bool(res.get("manual_fallback")) or (low_balance and not res.get("ok"))
    if switch_to_manual and cfg.get("mode") == "api":
        live_cfg = config.load()
        live_cfg.setdefault("zhuque", {})["mode"] = "manual"
        config.save(live_cfg)
    diagnostic = {
        "chapter": idx, "round": 1, "checked_at": db.now(),
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_chars": len(text), "applied": False, "source": "content",
        "adapter": cfg.get("adapter", "generic"), "ok": bool(res.get("ok")),
        "score": score, "balance": balance, "manual_fallback": manual_fallback,
        "labels_ratio": res.get("labels_ratio") or (res.get("diagnostics") or {}).get("labels_ratio") or {},
        "makers_usage_tokens": res.get("makers_usage_tokens"),
        "manual_mode_next": switch_to_manual,
        "metric": res.get("metric", "configured_score"),
        "diagnostics": res.get("diagnostics") or {}, "error": res.get("error", ""),
    }
    db.upsert_asset(pid, f"zhuque_{idx}_{task_id}_0", json.dumps(diagnostic, ensure_ascii=False))
    if balance is not None:
        db.upsert_asset(pid, "zhuque_balance_latest", json.dumps({"balance": balance, "checked_at": db.now()}, ensure_ascii=False))
    if not res.get("ok") or score is None:
        previous_score = row.get("zhuque_score")
        previous_status = row.get("status")
        if previous_score is not None and previous_status in {"checked", "medium_aigc"}:
            db.upsert_asset(pid, f"zhuque_{idx}_{task_id}_error", json.dumps(diagnostic, ensure_ascii=False))
            add_warning(pid, f"第 {idx} 章本次朱雀检测未成功（{res.get('error', '') or '未知原因'}），保留上次分数 {previous_score}。")
            return
        status = "await_check" if manual_fallback else "check_failed"
        db.execute("UPDATE generated SET zhuque_score=NULL, status=?, updated_at=? WHERE project_id=? AND idx=?",
                   (status, db.now(), pid, idx))
        db.upsert_asset(pid, f"zhuque_{idx}_latest", json.dumps(diagnostic, ensure_ascii=False))
        add_warning(pid, f"第 {idx} 章朱雀未完成自动过检：{res.get('error', '') or '请复制到朱雀网页检测并记录分数。'}")
        return

    original_text = text
    original_score = float(score)
    repair_target = float(cfg.get("repair_target") or 50)
    repair_rounds = int(cfg.get("repair_rounds") or cfg.get("max_rounds") or 0)
    if bool(cfg.get("repair_enabled", True)) and original_score >= repair_target and repair_rounds > 0:
        repaired_text, repaired_score, repaired_res, repaired = await _repair_flagged_segments(
            task_id, pid, idx, original_text, original_score, res, repair_target, repair_rounds)
        if repaired:
            try:
                _ensure_unchanged(pid, idx, row)
            except ValueError as exc:
                add_warning(pid, f"第 {idx} 章自动压缩未保存：{exc}")
            else:
                _backup_chapter(pid, idx, row, task_id, "auto_repair")
                db.execute("UPDATE generated SET content=?, polished='', zhuque_score=?, status=?, updated_at=? "
                           "WHERE project_id=? AND idx=?",
                           (repaired_text, repaired_score, _zhuque_status(repaired_score), db.now(), pid, idx))
                _invalidate_contexts(pid, idx)
                text = repaired_text
                score = repaired_score
                res = repaired_res
                balance = res.get("balance", balance)
                diagnostic.update({
                    "score": score,
                    "text_chars": len(text),
                    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "applied": True,
                    "diagnostics": res.get("diagnostics") or diagnostic.get("diagnostics") or {},
                    "makers_usage_tokens": res.get("makers_usage_tokens"),
                    "error": "",
                })
                add_warning(pid, f"第 {idx} 章已自动压缩高风险段：{original_score} → {score}")

    status = _zhuque_status(float(score))
    db.execute("UPDATE generated SET zhuque_score=?, status=?, updated_at=? WHERE project_id=? AND idx=?",
               (score, status, db.now(), pid, idx))
    db.upsert_asset(pid, f"zhuque_{idx}_latest", json.dumps(diagnostic, ensure_ascii=False))
    if switch_to_manual:
        add_warning(pid, f"朱雀积分余额低或调用额度不足（当前 {balance}），后续章节已切换网页手动检测。" +
                    ("本章 API 检测已成功。" if res.get("ok") else f"第 {idx} 章请复制正文到网页免费检测并记录分数。"))
    elif float(score) > threshold:
        add_warning(pid, f"第 {idx} 章朱雀 {score} 分（人工率 {max(0.0, 100 - float(score)):.1f}%），超过 60 分，暂不能生成下一章。")


async def _zhuque_loop(task_id: int, pid: int, idx: int, words: int,
                       continue_on_no_improvement: bool = False) -> None:
    """兼容旧调用名：检测当前正文一次，只记录报告，不按分数自动改写。"""
    del words, continue_on_no_improvement
    await _auto_zhuque_check(task_id, pid, idx)


async def _build_context(task_id: int, pid: int, b: int, group: list[dict],
                         constitution_text: str) -> dict:
    texts = []
    for r in group:
        row = _gen_row(pid, r["idx"])
        if row:
            texts.append(f"## 第{r['idx']}章 {row.get('title') or r.get('title', '')}\n{_chapter_text(row)}")
    if not texts:
        return {}
    combined = utils.clip("\n\n".join(texts), 26000)
    start, end = group[0]["idx"], group[-1]["idx"]
    plan_text = "\n".join(f"第{r['idx']}章《{r.get('title', '')}》：{r.get('event', '')}" for r in group)
    existing = db.q1("SELECT content FROM contexts WHERE project_id=? AND after_batch=?", (pid, b))
    old_deviations = []
    if existing:
        try:
            old_deviations = json.loads(existing["content"]).get("deviations") or []
        except (ValueError, TypeError, AttributeError):
            pass
    system, user = prompts.context_card(constitution_text, combined, start, end, plan_text, old_deviations)
    try:
        data, _ = await call_json("check", system, user, pid=pid, max_tokens=3200,
                                  temperature=0.3, tag=f"context_{b}")
    except Paused:
        raise
    except Exception as exc:  # 承接失败不阻塞，至少保留正文尾部
        data = {"context_card": "（承接卡生成失败，请检查模型配置）", "deviations": [],
                "character_states": [], "threads": [], "next_hook": ""}
        add_warning(pid, f"第 {b + 1} 批承接卡生成失败：{exc}")
    # 兼容旧模型字段，同时把偏离绑定到实际章节，供界面和下一批写作使用。
    normalized_deviations = []
    for item in data.get("deviations") or []:
        if not isinstance(item, dict) or not str(item.get("issue") or "").strip():
            continue
        raw_chapter = item.get("chapter", item.get("chapter_idx"))
        try:
            if isinstance(raw_chapter, str):
                number = re.search(r"(?:第\s*)?([0-9０-９零一二三四五六七八九十百千万两]+)\s*章?", raw_chapter)
                chapter = utils.cn_to_int(number.group(1)) if number else None
            else:
                chapter = int(raw_chapter) if raw_chapter is not None else None
        except (TypeError, ValueError):
            chapter = None
        if chapter is None:
            clue = f"{item.get('issue', '')} {item.get('evidence', '')}"
            number = re.search(r"第\s*([0-9０-９零一二三四五六七八九十百千万两]+)\s*章", clue)
            chapter = utils.cn_to_int(number.group(1)) if number else None
        if chapter is not None and not start <= chapter <= end:
            chapter = None
        normalized_deviations.append({
            "chapter": chapter,
            "issue": str(item.get("issue") or "").strip(),
            "evidence": str(item.get("evidence") or "").strip(),
            "level": str(item.get("level") or "中").strip(),
            "suggestion": str(item.get("suggestion") or "").strip(),
        })
    data["deviations"] = normalized_deviations
    db.execute(
        """INSERT INTO contexts(project_id, after_batch, content, deviation, updated_at)
           VALUES(?,?,?,?,?)
           ON CONFLICT(project_id, after_batch) DO UPDATE SET content=excluded.content,
             deviation=excluded.deviation, updated_at=excluded.updated_at""",
        (pid, b, json.dumps(data, ensure_ascii=False),
         json.dumps(normalized_deviations, ensure_ascii=False), db.now()),
    )
    return data


def _clean_title(title: str) -> str:
    return utils.clean_chapter_title(title)


def _writing_memory(pid: int, idx: int) -> tuple[str, str, str]:
    """每章重新读取大纲与正文；不把计划、参考书或未来章节当作已发生剧情。"""
    row = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    outline = (row or {}).get("content") or ""
    if not outline.strip():
        raise ValueError(f"第 {idx} 章生成前未找到原创大纲，请先保存大纲再继续。")
    rows = db.q(
        "SELECT idx,title,content,polished FROM generated "
        "WHERE project_id=? AND idx<? AND (idx<=5 OR idx>=?) ORDER BY idx",
        (pid, idx, max(1, idx - 5)),
    )
    chapters = {}
    for chapter in rows:
        body = (chapter.get("polished") or "").strip() or (chapter.get("content") or "").strip()
        if body:
            chapters[chapter["idx"]] = (chapter, body)
    required = set(range(1, min(idx, 6))) | set(range(max(1, idx - 5), idx))
    missing = sorted(required - chapters.keys())
    if missing:
        numbers = "、".join(str(n) for n in missing)
        raise ValueError(f"第 {idx} 章生成前缺少第 {numbers} 章正文，无法读取前序剧情记忆，请先补齐这些章节。")

    def render(number: int, opening: bool = False) -> str:
        chapter, body = chapters[number]
        # 最近五章完整送入模型；更早的开篇章保留首、中、尾，避免长篇重复挤占上下文。
        if opening and len(body) > 2400:
            middle = len(body) // 2
            body = (body[:800] + "\n（原文节选：中段）\n" + body[middle - 400:middle + 400]
                    + "\n（原文节选：结尾）\n" + body[-800:])
        return f"第{number}章《{chapter.get('title') or ''}》已发生剧情（正文原文）：\n{body}"

    recent = [n for n in chapters if n >= max(1, idx - 5)]
    opening_parts = []
    for n in range(1, min(idx, 6)):
        if n in recent:
            opening_parts.append(f"第{n}章开篇记忆：见下方最近五章中的完整正文（同一章，不是新事件）。")
        else:
            opening_parts.append(render(n, opening=True))
    return outline, "\n\n".join(opening_parts), "\n\n".join(render(n) for n in recent)


def _chapter_memory(pid: int, idx: int, constitution_text: str,
                    plan_row: dict | None = None, *, editing: bool = False) -> str:
    """写作、重写、自然编辑与校准共用事实来源；未来正文只用于编辑时的衔接边界。"""
    outline, opening, recent = _writing_memory(pid, idx)
    plan = utils.parse_plan(outline)
    plan_row = plan_row or next((r for r in plan if r["idx"] == idx), None)
    if not plan_row or not str(plan_row.get("event") or "").strip():
        raise ValueError(f"第 {idx} 章缺少可用的章节计划，请先补提取章节计划。")
    size = int(config.load()["writing"].get("batch_size") or 5)
    batch = next((b for b, g in enumerate(_plan_groups(plan, size))
                  if any(r["idx"] == idx for r in g)), 0)
    ctx = _last_context(pid, batch)
    prev = _gen_row(pid, idx - 1)
    tail = _chapter_text(prev)[-2200:] if prev else ""
    next_row = _gen_row(pid, idx + 1) if editing else None
    following = (_chapter_text(next_row)[:1800] if next_row else "")
    return (
        f"【创作宪法】\n{constitution_text}\n\n【完整原创大纲（未来计划不是事实）】\n{outline}\n\n"
        f"【本章计划：第{idx}章《{plan_row.get('title', '')}》】\n"
        f"核心事件：{plan_row.get('event', '')}\n冲突/爽点：{plan_row.get('payoff', '')}\n"
        f"结尾钩子：{plan_row.get('hook', '')}\n\n"
        f"【开篇前五章已发生剧情】\n{opening or '（无）'}\n\n"
        f"【最近五章完整正文（既成事实）】\n{recent or '（全书第一章）'}\n\n"
        f"【上一章结尾原文】\n{tail or '（无）'}\n\n"
        f"【上一批承接卡（建议需核对，不可推翻正文）】\n{_context_text(ctx) or '（无）'}\n\n"
        f"【紧邻后文开头（只用于保持改稿衔接，禁止搬入本章）】\n{following or '（无）'}"
    )


class ChapterOutputError(ValueError):
    """模型输出为空、明显截断或串章；不作剧情判断。"""


def _candidate_body(text: str, idx: int, original: str = "") -> str:
    text = re.sub(r"^```(?:markdown|text)?\s*\n", "", text.strip())
    text = re.sub(r"\n```\s*$", "", text)
    headings = list(utils.CHAPTER_HEAD_RE.finditer(text))
    if headings and (len(headings) != 1 or utils.cn_to_int(headings[0].group(1)) != idx):
        raise ChapterOutputError(f"第 {idx} 章返回了其他章号或多章内容")
    parsed = utils.parse_generated(text)
    body = parsed[0]["content"] if parsed else ("" if headings else text)
    minimum = max(300, int(utils.word_count(original) * 0.65))
    if utils.word_count(body) < minimum:
        raise ChapterOutputError(f"第 {idx} 章候选正文过短（{utils.word_count(body)} 字，至少 {minimum} 字），可能截断或删失剧情")
    return body


async def _prepare_candidate(task_id: int, pid: int, idx: int, text: str,
                             original: str = "") -> str:
    """只解析正文与章号，不再调用模型审查或阻拦剧情。"""
    db.upsert_asset(pid, f"chapter_output_{idx}_candidate", text)
    tick(task_id)
    try:
        return _candidate_body(text, idx, original)
    except ChapterOutputError as exc:
        message = f"{exc}；原有正文未覆盖。"
        add_warning(pid, message)
        raise ChapterOutputError(message) from exc


def _backup_chapter(pid: int, idx: int, row: dict, task_id: int, kind: str) -> None:
    db.upsert_asset(pid, f"chapter_{idx}_{kind}_{task_id}_before", json.dumps({
        "idx": idx, "title": row.get("title"), "content": row.get("content"),
        "polished": row.get("polished"), "status": row.get("status"),
        "zhuque_score": row.get("zhuque_score"), "saved_at": db.now(),
    }, ensure_ascii=False))


def _ensure_unchanged(pid: int, idx: int, before: dict | None) -> None:
    current = _gen_row(pid, idx)
    if (current is None) != (before is None) or (current and any(
            current.get(k) != before.get(k) for k in ("title", "content", "polished"))):
        raise ValueError(f"第 {idx} 章在生成期间已被修改；候选稿已暂存，请基于最新正文重试。")


async def write_one_chapter(task_id: int, pid: int, plan_row: dict, group: list[dict],
                            b: int, constitution_text: str, book: str, words: int,
                            ctx: dict) -> None:
    idx = plan_row["idx"]
    before = _gen_row(pid, idx)
    outline, opening_anchor, continuity_anchor = _writing_memory(pid, idx)
    prev = _prev_chapter(pid, idx)
    prev_tail = _chapter_text(prev)[-2200:] if prev else ""
    upcoming = "\n".join(
        f"第{r['idx']}章《{r.get('title', '')}》：{r.get('event', '')}"
        for r in group if r["idx"] > idx
    )
    so_far_parts = []
    for r in group:
        if r["idx"] >= idx:
            break
        prev_row = _gen_row(pid, r["idx"])
        if prev_row and prev_row.get("content"):
            t = _chapter_text(prev_row)
            so_far_parts.append(
                f"第{r['idx']}章《{prev_row.get('title') or r.get('title', '')}》"
                f"开头：{t[:100]}……结尾：{t[-220:]}"
            )
    batch_so_far = "\n".join(so_far_parts)
    system, user = prompts.write_chapter(
        constitution_text, book, plan_row, upcoming, prev_tail,
        _context_text(ctx), _states_text(ctx), words, batch_so_far,
        outline=outline, opening_anchor=opening_anchor, continuity_anchor=continuity_anchor,
    )
    tick(task_id, message=f"已读取大纲和前序剧情，正在写第 {idx} 章《{plan_row.get('title', '')}》")
    text = await llm.chat("write", system, user, project_id=pid,
                          max_tokens=min(8000, max(3000, int(words * 2.2))),
                          temperature=0.82)
    body = await _prepare_candidate(task_id, pid, idx, text)
    title = _clean_title(plan_row.get("title", ""))
    _ensure_unchanged(pid, idx, before)
    if before:
        _backup_chapter(pid, idx, before, task_id, "write")
    db.execute(
        """INSERT INTO generated(project_id, idx, title, content, polished, batch_idx, status, updated_at)
           VALUES(?,?,?,?, '', ?, 'draft', ?)
           ON CONFLICT(project_id, idx) DO UPDATE SET title=excluded.title,
             content=excluded.content, polished='', status='draft', zhuque_score=NULL,
             updated_at=excluded.updated_at""",
        (pid, idx, title, body, b, db.now()),
    )


async def run_write(task_id: int, pid: int, payload: dict) -> None:
    book = new_book_title(pid)  # 正文写的是新书，不是参考书
    cons = _get_constitution(pid)
    outline_row = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    if not outline_row or not outline_row["content"]:
        raise ValueError("还没有原创大纲。")
    plan = await prepare_chapter_plan(pid, task_id)
    cfg = config.load()
    size = int(cfg["writing"].get("batch_size") or 5)
    words = int(cfg["writing"].get("words_per_chapter") or 2600)
    groups = _plan_groups(plan, size)

    mode = payload.get("mode", "next")
    # 正文永远以“章”为最小任务单位。每个写作任务只生成一章，
    # 用户检查、复制检测或修改后，再决定是否开始下一章。
    chapter_map = {int(row["idx"]): (b, group, row)
                   for b, group in enumerate(groups) for row in group}
    if payload.get("idx") is not None:
        requested = [int(payload["idx"])]
    elif payload.get("batch") is not None:  # 兼容旧客户端：单次只处理该批一章
        group = groups[int(payload["batch"])] if 0 <= int(payload["batch"]) < len(groups) else []
        if payload.get("force"):
            requested = [int(row["idx"]) for row in group[:1]]
        else:
            requested = [int(row["idx"]) for row in group
                         if not ((_gen_row(pid, int(row["idx"])) or {}).get("content") or "").strip()][:1]
    elif mode == "all":  # 兼容旧任务：降级为只写下一章
        requested = [int(row["idx"]) for row in plan
                     if not ((_gen_row(pid, int(row["idx"])) or {}).get("content") or "").strip()][:1]
    elif mode == "range":
        start_idx = int(payload.get("start", 1))
        end_idx = int(payload.get("end", start_idx))
        requested = [int(row["idx"]) for row in plan if start_idx <= int(row["idx"]) <= end_idx]
    else:
        requested = [int(row["idx"]) for row in plan
                     if not ((_gen_row(pid, int(row["idx"])) or {}).get("content") or "").strip()][:1]
    targets = [idx for idx in requested if idx in chapter_map]
    if not targets:
        tick(task_id, progress=1, total=1, message="所有章节都已经写完了")
        return

    force = bool(payload.get("force"))
    if not force:
        targets = [idx for idx in targets
                   if not ((_gen_row(pid, idx) or {}).get("content") or "").strip()]
    # 防止旧版客户端或恢复任务携带 all/range/batch 时一次跑多章。
    targets = targets[:1]
    if not targets:
        tick(task_id, progress=1, total=1, message="所有章节都已经写完了")
        return
    total_chapters = len(targets)
    done = 0
    for idx in targets:
        b, group, row_plan = chapter_map[idx]
        # 章节按顺序推进，避免跳过上一章导致记忆缺口。
        if idx > 1:
            previous = _gen_row(pid, idx - 1)
            if not previous or not (previous.get("content") or "").strip():
                raise ValueError(f"第 {idx} 章之前还有未生成的正文，请先完成第 {idx - 1} 章。")
            if not _chapter_passed(previous, cfg.get("zhuque", {})):
                score = previous.get("zhuque_score")
                raise ValueError(f"第 {idx - 1} 章朱雀分数必须不高于 60 分（当前 {score if score is not None else '未检测'}），请先完成检测。")
        ctx = _last_context(pid, b)
        if b > 0 and not ctx and _batch_complete(pid, groups[b - 1]):
            tick(task_id, message=f"缺少第 {b} 批承接卡，正在补建")
            ctx = await _build_context(task_id, pid, b - 1, groups[b - 1], cons["content"])
        db.set_batch(pid, "write", b, "running")
        try:
            await write_one_chapter(task_id, pid, row_plan, group, b, cons["content"],
                                    book, words, ctx)
            # 通过正式写作任务落库后自动检测一次；单元级写作函数仍可独立复用。
            await _auto_zhuque_check(task_id, pid, idx)
            done += 1
            tick(task_id, progress=done, total=total_chapters,
                 message=f"第 {idx} 章完成（{done}/{total_chapters}），已保存，可继续下一章")
            # 承接卡仍按原有分析批次维护，但只有该批最后一章完成后才生成。
            if _batch_complete(pid, group):
                tick(task_id, message=f"第 {b + 1} 批章节已齐，正在生成承接卡")
                await _build_context(task_id, pid, b, group, cons["content"])
                db.set_batch(pid, "write", b, "done")
            else:
                db.set_batch(pid, "write", b, "pending")
            db.execute("UPDATE projects SET status='writing', updated_at=? WHERE id=?", (db.now(), pid))
        except Paused:
            db.set_batch(pid, "write", b, "paused")
            raise
        except llm.LLMError:
            db.set_batch(pid, "write", b, "failed", "模型调用失败")
            raise
        except Exception as exc:  # noqa: BLE001
            db.set_batch(pid, "write", b, "failed", str(exc)[:300])
            raise
    tick(task_id, progress=total_chapters, total=total_chapters, message="写作任务完成")


# ---------------------------------------------------------------- 检测 / 重写 / 优化

async def run_check(task_id: int, pid: int, payload: dict) -> None:
    idxs = payload.get("idxs") or []
    if not idxs:
        rows = db.q("SELECT idx FROM generated WHERE project_id=? ORDER BY idx", (pid,))
        idxs = [r["idx"] for r in rows]
    words = int(config.load()["writing"].get("words_per_chapter") or 2600)
    failed = 0
    for i, idx in enumerate(idxs, 1):
        tick(task_id, progress=i - 1, total=len(idxs), message=f"检测第 {idx} 章")
        await _auto_zhuque_check(task_id, pid, int(idx))
        row = _gen_row(pid, int(idx))
        is_failed = row and row.get("status") == "check_failed"
        failed += int(bool(is_failed))
        tick(task_id, progress=i, message=f"第 {idx} 章检测失败，正文已保留" if is_failed else f"第 {idx} 章检测完成")
    if failed:
        raise ValueError(f"{failed} 章检测失败，正文已保留；请查看总览的具体原因，在设置中测试接口后重试。")


async def run_rewrite(task_id: int, pid: int, payload: dict) -> None:
    idx = int(payload.get("idx") or 0)
    row = _gen_row(pid, idx)
    if not row:
        raise ValueError(f"第 {idx} 章还不存在。")
    book_title = new_book_title(pid)
    cons = _get_constitution(pid)
    naturalize = payload.get("style") == "natural"
    instruction = prompts.NATURAL_EDIT_INSTRUCTION if naturalize else (payload.get("instruction") or "")
    prev = _prev_chapter(pid, idx)
    prev_tail = _chapter_text(prev)[-1800:] if prev else ""
    memory = _chapter_memory(pid, idx, cons["content"], editing=True)
    action = "自然点化" if naturalize else "重写"
    tick(task_id, progress=0, total=2, message=f"已读取大纲、章节计划及前五章剧情，正在{action}第 {idx} 章")
    system, user = prompts.rewrite_chapter(book_title, cons["content"], _chapter_text(row),
                                           instruction, prev_tail, memory=memory)
    words = int(config.load()["writing"].get("words_per_chapter") or 2600)
    text = await llm.chat("polish", system, user, project_id=pid,
                          max_tokens=min(8000, max(3000, int(words * 2.2))), temperature=0.75)
    body = await _prepare_candidate(task_id, pid, idx, text, _chapter_text(row))
    _ensure_unchanged(pid, idx, row)
    _backup_chapter(pid, idx, row, task_id, "natural_edit" if naturalize else "rewrite")
    _invalidate_contexts(pid, idx)
    db.execute("UPDATE generated SET content=?, polished='', zhuque_score=NULL, status='rewritten', updated_at=? "
               "WHERE project_id=? AND idx=?", (body, db.now(), pid, idx))
    zhuque_cfg = config.load().get("zhuque", {})
    await _auto_zhuque_check(task_id, pid, idx)
    tick(task_id, progress=1, message="重写完成，朱雀结果已记录或已转人工网页检测")
    outline_row = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    plan = utils.parse_plan((outline_row or {}).get("content", ""))
    size = int(config.load()["writing"].get("batch_size") or 5)
    for b, group in enumerate(_plan_groups(plan, size)):
        if any(r["idx"] == idx for r in group):
            await _build_context(task_id, pid, b, group, cons["content"])
            break
    fresh = _gen_row(pid, idx) or {}
    score = fresh.get("zhuque_score")
    if score is not None:
        human = max(0.0, min(100.0, 100 - float(score)))
        if float(score) < 50:
            result_note = f"朱雀 {score} 分，人工率 {human:.1f}%，已通过"
        elif float(score) <= 60:
            result_note = f"朱雀 {score} 分，人工率 {human:.1f}%，疑似人工，可继续"
        else:
            result_note = f"朱雀 {score} 分，人工率 {human:.1f}%，超过 60%，需先处理"
    else:
        result_note = "等待完成检测"
    tick(task_id, progress=2, total=2, message=f"{action}完成，{result_note}；承接卡已更新")


def _invalidate_contexts(pid: int, idx: int) -> None:
    plan = utils.parse_plan((db.q1("SELECT content FROM outline WHERE project_id=?", (pid,)) or {}).get("content", ""))
    size = int(config.load()["writing"].get("batch_size") or 5)
    batch = next((b for b, g in enumerate(_plan_groups(plan, size)) if any(r["idx"] == idx for r in g)), None)
    if batch is not None:
        # 被改章所在批次及后续卡片已过时；不能再把旧稿提炼出的状态注入新写作。
        db.execute("DELETE FROM contexts WHERE project_id=? AND after_batch>=?", (pid, batch))


def _realign_backup(pid: int, batch: int, idx: int, row: dict) -> None:
    key = f"realign_{batch}_{idx}_before"
    if not db.get_asset(pid, key):
        db.upsert_asset(pid, key, json.dumps({
            "idx": idx, "title": row.get("title", ""), "content": row.get("content", ""),
            "saved_at": db.now(),
        }, ensure_ascii=False))


async def realign_batch(task_id: int, pid: int, batch: int) -> None:
    """按主题和章节计划校准本批偏离章节，再重建承接卡。"""
    cons = _get_constitution(pid)
    outline_row = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    outline = (outline_row or {}).get("content", "")
    plan = utils.parse_plan(outline)
    size = int(config.load()["writing"].get("batch_size") or 5)
    groups = _plan_groups(plan, size)
    if batch < 0 or batch >= len(groups):
        raise ValueError(f"第 {batch + 1} 批不存在。")
    group = groups[batch]
    ctx_row = db.q1("SELECT content FROM contexts WHERE project_id=? AND after_batch=?", (pid, batch))
    try:
        ctx = json.loads(ctx_row["content"]) if ctx_row and ctx_row.get("content") else {}
    except (ValueError, TypeError):
        ctx = {}
    deviations = ctx.get("deviations") or []
    by_idx = {}
    for deviation in deviations:
        try:
            idx = int(deviation.get("chapter"))
        except (TypeError, ValueError):
            continue
        if any(row["idx"] == idx for row in group):
            by_idx.setdefault(idx, []).append(deviation)
    targets = [row for row in group if row["idx"] in by_idx and _gen_row(pid, row["idx"])]
    if not targets:
        raise ValueError("当前承接卡没有可定位的偏离章节，请先重新承接并定位偏离。")
    words = int(config.load()["writing"].get("words_per_chapter") or 2600)
    total = len(targets)
    staged = []
    for n, plan_row in enumerate(targets, 1):
        idx = plan_row["idx"]
        row = _gen_row(pid, idx)
        if not row or not (row.get("content") or "").strip():
            continue
        _realign_backup(pid, batch, idx, row)
        prev = _prev_chapter(pid, idx)
        next_row = _gen_row(pid, idx + 1)
        deviation = {
            "issue": "；".join(d.get("issue", "") for d in by_idx[idx]),
            "evidence": "；".join(d.get("evidence", "") for d in by_idx[idx]),
            "suggestion": "；".join(d.get("suggestion", "") for d in by_idx[idx]),
            "level": max((d.get("level", "中") for d in by_idx[idx]), key=lambda x: {"轻": 1, "中": 2, "重": 3}.get(x, 2)),
        }
        system, user = prompts.realign_chapter(
            new_book_title(pid), cons["content"], outline, plan_row, deviation,
            _chapter_text(row), _chapter_text(prev)[-2200:] if prev else "",
            _chapter_text(next_row)[:1800] if next_row else "",
            memory=_chapter_memory(pid, idx, cons["content"], plan_row, editing=True),
        )
        tick(task_id, progress=n - 1, total=total, message=f"正在按主题校准第 {idx} 章（{n}/{total}）")
        text = await llm.chat("polish", system, user, project_id=pid,
                              max_tokens=min(10000, max(4000, int(words * 2.5))), temperature=0.78)
        body = await _prepare_candidate(task_id, pid, idx, text, _chapter_text(row))
        _ensure_unchanged(pid, idx, row)
        _backup_chapter(pid, idx, row, task_id, "realign")
        _invalidate_contexts(pid, idx)
        # 顺序落库：校准下一章时读取上一章刚保存的版本。
        db.execute("UPDATE generated SET content=?, polished='', status='draft', zhuque_score=NULL, updated_at=? WHERE project_id=? AND idx=?",
                   (body, db.now(), pid, idx))
        # 校准结果直接保存，不自动调用朱雀。
        staged.append(idx)
    if not staged:
        raise ValueError("没有得到可用的校准正文，原稿均已保留。")
    tick(task_id, message=f"{len(staged)} 章校准稿已保存，正在更新承接卡")
    await _build_context(task_id, pid, batch, group, cons["content"])
    tick(task_id, progress=len(staged), total=total, message=f"已校准 {len(staged)} 章，承接卡已更新")


async def run_polish(task_id: int, pid: int, payload: dict) -> None:
    cons = _get_constitution(pid)
    book_title = new_book_title(pid)
    rows = db.q("SELECT idx FROM generated WHERE project_id=? AND content!='' ORDER BY idx", (pid,))
    idxs = [r["idx"] for r in rows]
    if payload.get("start") is not None:
        idxs = [i for i in idxs if i >= int(payload["start"])]
    if payload.get("end") is not None:
        idxs = [i for i in idxs if i <= int(payload["end"])]
    if not idxs:
        raise ValueError("没有可优化的章节。")
    words = int(config.load()["writing"].get("words_per_chapter") or 2600)
    changed = []
    for i, idx in enumerate(idxs, 1):
        tick(task_id, progress=i - 1, total=len(idxs), message=f"正在优化第 {idx} 章")
        row = _gen_row(pid, idx)
        if not row:
            continue
        memory = _chapter_memory(pid, idx, cons["content"], editing=True)
        system, user = prompts.polish_batch(book_title, cons["content"], _chapter_text(row), idx, idx, memory=memory)
        text = await llm.chat("polish", system, user, project_id=pid,
                              max_tokens=min(8000, max(3000, int(words * 2.2))),
                              temperature=0.85)
        try:
            body = await _prepare_candidate(task_id, pid, idx, text, _chapter_text(row))
        except ChapterOutputError:
            continue
        _ensure_unchanged(pid, idx, row)
        _backup_chapter(pid, idx, row, task_id, "polish")
        _invalidate_contexts(pid, idx)
        db.execute("UPDATE generated SET polished=?, zhuque_score=NULL, status='polished', updated_at=? "
                   "WHERE project_id=? AND idx=?", (body, db.now(), pid, idx))
        changed.append(idx)
        tick(task_id, progress=i, message=f"第 {idx} 章优化完成")
    plan = utils.parse_plan((db.q1("SELECT content FROM outline WHERE project_id=?", (pid,)) or {}).get("content", ""))
    size = int(config.load()["writing"].get("batch_size") or 5)
    for b, group in enumerate(_plan_groups(plan, size)):
        if any(r["idx"] in changed for r in group):
            await _build_context(task_id, pid, b, group, cons["content"])
    if len(changed) == len(idxs):
        db.execute("UPDATE projects SET status='polished', updated_at=? WHERE id=?", (db.now(), pid))
    tick(task_id, progress=len(idxs), total=len(idxs), message=f"优化完成：{len(changed)} 章已更新；{len(idxs) - len(changed)} 章保留原稿，具体原因见警告")


async def refresh_context(task_id: int, pid: int, payload: dict) -> None:
    b = int(payload.get("batch") or 0)
    cons = _get_constitution(pid)
    plan = utils.parse_plan((db.q1("SELECT content FROM outline WHERE project_id=?", (pid,)) or {}).get("content", ""))
    size = int(config.load()["writing"].get("batch_size") or 5)
    groups = _plan_groups(plan, size)
    if not groups:
        raise ValueError("没有章节计划。")
    group = groups[min(b, len(groups) - 1)]
    await _build_context(task_id, pid, b, group, cons["content"])
    tick(task_id, progress=1, total=1, message="承接卡已重建")


# ---------------------------------------------------------------- 导出

def export_text(pid: int, use_polished: bool = True) -> str:
    outline = db.q1("SELECT content FROM outline WHERE project_id=?", (pid,))
    title = new_book_title(pid)
    if outline and outline["content"]:
        m = re.search(r"^#\s+(.+)$", outline["content"], re.M)
        if m:
            title = m.group(1).strip()
    rows = db.q("SELECT * FROM generated WHERE project_id=? ORDER BY idx", (pid,))
    parts = [title, ""]
    for r in rows:
        text = _chapter_text(r, use_polished) if use_polished else r["content"]
        if not text:
            continue
        ch_title = _clean_title(r.get("title") or "")
        parts.append(f"## 第{r['idx']}章 {ch_title}".strip())
        parts.append(text.strip())
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------- 任务分发

async def dispatch(row: dict) -> None:
    pid = row.get("project_id")
    payload = json.loads(row.get("payload") or "{}")
    type_ = row["type"]
    if type_ == "analyze":
        await run_analysis(row["id"], int(pid), payload)
    elif type_ == "outline":
        await run_outline(row["id"], int(pid), payload)
    elif type_ == "plan":
        await run_plan(row["id"], int(pid), payload)
    elif type_ == "write":
        await run_write(row["id"], int(pid), payload)
    elif type_ == "check":
        await run_check(row["id"], int(pid), payload)
    elif type_ == "rewrite":
        await run_rewrite(row["id"], int(pid), payload)
    elif type_ == "realign":
        await realign_batch(row["id"], int(pid), int(payload.get("batch", 0)))
    elif type_ == "polish":
        await run_polish(row["id"], int(pid), payload)
    elif type_ == "context":
        await refresh_context(row["id"], int(pid), payload)
    else:
        raise ValueError(f"未知任务类型：{type_}")
