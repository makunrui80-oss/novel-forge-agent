"""TXT 解码与分章。"""
from __future__ import annotations

import re

CHAPTER_RE = re.compile(
    r"(?m)^[ \t　]*(第[零一二三四五六七八九十百千万两0-9０-９]{1,12}[章回节篇][^\n]{0,60})[ \t　]*$"
)
VOLUME_RE = re.compile(r"(?m)^[ \t　]*(第[零一二三四五六七八九十百千万两0-9]{1,12}[卷部][^\n]{0,40})$")


def decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "utf-16"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="ignore")


def split_chapters(text: str, fallback_size: int = 3500) -> list[dict]:
    chapters, _ = split_chapters_with_diagnostics(text, fallback_size)
    return chapters


def split_chapters_with_diagnostics(text: str, fallback_size: int = 3500) -> tuple[list[dict], dict]:
    """分章并标记目录目录项，避免把目录标题当作空正文批次。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    # 卷名是结构标签，不应被并入上一章正文或单独显示为空章节。
    text = VOLUME_RE.sub("", text).strip()
    if not text:
        return [], {"toc_headings_removed": 0, "front_matter_removed": False}
    matches = list(CHAPTER_RE.finditer(text))
    if len(matches) < 2:
        # 没有规范章节号：按长度切块，标题用序号
        chunks: list[dict] = []
        i = 0
        while i < len(text):
            piece = text[i:i + fallback_size]
            chunks.append({"title": f"第{len(chunks) + 1}节", "content": piece.strip()})
            i += fallback_size
        return chunks, {"toc_headings_removed": 0, "front_matter_removed": False}

    chapters: list[dict] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        # 去掉正文开头重复的章节名
        first_line = body.split("\n", 1)[0].strip() if body else ""
        if first_line and first_line == title:
            body = body.split("\n", 1)[1].strip() if "\n" in body else ""
        if len(body) < 30 and i + 1 < len(matches):
            # 空章（只有标题），跳过但不合并，避免正文错位
            chapters.append({"title": title, "content": body})
            continue
        chapters.append({"title": title, "content": body})

    # TXT 常在开头放书名、简介和目录，目录标题会被误认为空章节；
    # 真正正文从后面重复出现的章节标题开始。先识别并去掉出版信息，
    # 再检查实际章节序列的开头，避免简介挡住目录检测。
    preface = text[:matches[0].start()].strip()
    metadata_labels = sum(bool(re.search(rf"(?m)^\s*{label}\s*[：:]", preface))
                          for label in ("书名", "作者", "标签", "简介"))
    front_matter_removed = metadata_labels >= 2

    # 目录通常是一串只有标题的章节行，后面再重复出现带正文的真实章节。
    # 仅当开头至少 5 条都是空标题、且多数标题在后文出现正文时才跳过，
    # 这样单个缺正文的真实章节仍会保留，不会把正常章节号整体改乱。
    empty_prefix = 0
    for chapter in chapters:
        if len((chapter.get("content") or "").strip()) >= 30:
            break
        empty_prefix += 1
    normalized_later_titles = {
        _normalize_title(chapter["title"])
        for chapter in chapters[empty_prefix:]
        if len((chapter.get("content") or "").strip()) >= 30
    }
    matched_toc = sum(
        _normalize_title(chapter["title"]) in normalized_later_titles
        for chapter in chapters[:empty_prefix]
    )
    toc_removed = 0
    if empty_prefix >= 5 and matched_toc >= max(3, (empty_prefix + 1) // 2):
        chapters = chapters[empty_prefix:]
        toc_removed = empty_prefix

    # 没有明显出版元数据、且内容足够长时，才将章节前文字视为序章。
    if len(preface) > 200 and not front_matter_removed:
        chapters.insert(0, {"title": "序章", "content": preface})
    return chapters, {"toc_headings_removed": toc_removed,
                      "front_matter_removed": front_matter_removed}


def _normalize_title(title: str) -> str:
    return re.sub(r"[\s　\t]+", "", str(title or "")).strip()


def batch_of(idx: int, batch_size: int) -> int:
    return (idx - 1) // batch_size
