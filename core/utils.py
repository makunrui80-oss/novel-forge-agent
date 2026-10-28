"""通用工具：JSON 容错解析、章节解析、字数统计。"""
from __future__ import annotations

import json
import re

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str) -> int | None:
    s = s.strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section, number = 0, 0, 0
    units = {"十": 10, "百": 100, "千": 1000}
    for ch in s:
        if ch in CN_NUM:
            number = CN_NUM[ch]
        elif ch in units:
            unit = units[ch]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        elif ch == "万":
            total += (section + number) * 10000
            section, number = 0, 0
        else:
            return None
    return total + section + number or None


def extract_json(text: str):
    """从模型输出里尽量抠出 JSON。"""
    if not text:
        return None
    t = text.strip()
    # 去掉代码块围栏
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    # 只解析最外层；截断对象里的一个 chapter 不能冒充整份结果。
    starts = [pos for pos in (t.find("{"), t.find("[")) if pos >= 0]
    if starts:
        candidate = t[min(starts):]
        try:
            obj, _ = json.JSONDecoder().raw_decode(candidate)
            return obj
        except (ValueError, TypeError):
            pass
    # 尝试修复尾逗号
    fixed = re.sub(r",\s*([}\]])", r"\1", t)
    try:
        return json.loads(fixed)
    except Exception:
        return None


def recover_detail(text: str) -> dict | None:
    """仅回收截断细纲中已经完整输出的摘要，不补写任何原文或推理。"""
    start = re.search(r'"chapters"\s*:\s*\[', text or "")
    if not start:
        return None
    chapters = []
    # 即使 beats 或其后的字段被截断，已闭合的 summary 字符串仍可使用。
    quoted = r'"(?:\\.|[^"\\])*"'
    body = text[start.end():]
    matches = list(re.finditer(r'\{\s*"(?:idx|title|summary)"', body))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        section = body[match.start():end]
        row = {}
        for key in ("idx", "title", "summary"):
            value = re.search(r'"' + key + r'"\s*:\s*(' + quoted + r'|\d+)', section)
            if value:
                try:
                    row[key] = json.loads(value.group(1))
                except ValueError:
                    pass
        if isinstance(row.get("summary"), str) and row["summary"].strip():
            chapters.append(row)
    return {"chapters": chapters, "batch_notes": {}, "takeaways": [],
            "partial_output": True} if chapters else None


CHAPTER_HEAD_RE = re.compile(
    r"(?m)^[ \t]*#{0,4}[ \t]*第[ \t]*([0-9０-９零一二三四五六七八九十百千万两]+)[ \t]*章[ \t]*[：:、.．]?[ \t]*(.*?)[ \t]*$"
)


def parse_generated(text: str) -> list[dict]:
    """解析模型返回的 5 章正文。"""
    matches = list(CHAPTER_HEAD_RE.finditer(text or ""))
    if not matches:
        return []
    out = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        idx = cn_to_int(m.group(1))
        title = (m.group(2) or "").strip("# 　")
        body = text[m.end():end].strip()
        if not body:
            continue
        out.append({"idx": idx, "title": title, "content": body})
    return out


def clean_chapter_title(title: str) -> str:
    return re.sub(r"^第\s*[0-9０-９零一二三四五六七八九十百千万两]+\s*章[：:、.．\s]*", "",
                  (title or "").strip()).strip()


def parse_plan(outline: str) -> list[dict]:
    """从大纲中解析章节计划。"""
    if not outline:
        return []
    m = re.search(r"<!--\s*PLAN\s*(.*?)(?:\s*PLAN\s*-->|-->|\Z)", outline, re.S | re.I)
    block = m.group(1) if m else outline
    rows: list[dict] = []
    if block:
        for line in block.splitlines():
            line = line.strip().replace("｜", "|")
            line = line.strip("|").strip()
            if not line or "|" not in line:
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            number = re.fullmatch(r"(?:第\s*)?([0-9０-９零一二三四五六七八九十百千万两]+)(?:\s*章)?", parts[0])
            if not number or not parts[2]:
                continue
            idx = cn_to_int(number.group(1))
            if idx is None or idx < 1:
                continue
            rows.append({
                "idx": idx,
                "title": clean_chapter_title(parts[1]),
                "event": parts[2] if len(parts) > 2 else "",
                "payoff": parts[3] if len(parts) > 3 else "",
                "hook": parts[4] if len(parts) > 4 else "",
            })
    if rows:
        return sorted({r["idx"]: r for r in rows}.values(), key=lambda r: r["idx"])
    # 退化：解析 Markdown 表格
    for line in outline.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.match(r"^[0-9]+$", cells[0]):
            continue
        idx = int(cells[0])
        rows.append({"idx": idx, "title": clean_chapter_title(cells[1]), "event": cells[2],
                     "payoff": cells[3] if len(cells) > 3 else "",
                     "hook": cells[4] if len(cells) > 4 else ""})
    if rows:
        return rows
    # 模型可能用章节标题和段落描述计划，不要求用户手写 PLAN 标记。
    for chapter in parse_generated(outline):
        body = chapter["content"]
        event = re.search(r"(?:核心事件|主要内容|剧情|梗概)[：:]\s*([^\n]+)", body)
        rows.append({"idx": chapter["idx"], "title": clean_chapter_title(chapter["title"]),
                     "event": event.group(1).strip() if event else body[:600],
                     "payoff": "", "hook": ""})
    return sorted({r["idx"]: r for r in rows if r["idx"]}.values(), key=lambda r: r["idx"])


def attach_plan(outline: str, rows: list[dict]) -> str:
    """把已提取的计划保存为稳定格式，正文和旧版严格解析器都可读取。"""
    lines = []
    for row in rows:
        cells = [str(row.get(key) or "").replace("|", "／").replace("\n", " ").strip()
                 for key in ("idx", "title", "event", "payoff", "hook")]
        lines.append("|".join(cells))
    match = re.search(r"<!--\s*PLAN\b.*?(?:PLAN\s*-->|-->|\Z)", outline, re.S | re.I)
    block = "<!--PLAN\n" + "\n".join(lines) + "\nPLAN-->"
    if match:
        return outline[:match.start()] + block + outline[match.end():]
    return outline.rstrip() + "\n\n## 可执行章节计划\n\n" + block + "\n"


def plan_rows_text(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        extra = "；".join(x for x in [r.get("payoff"), r.get("hook")] if x)
        lines.append(f"第{r['idx']}章《{r['title']}》：{r.get('event', '')}" + (f"（{extra}）" if extra else ""))
    return "\n".join(lines)


def word_count(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def clip(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "\n……（以下省略）"
