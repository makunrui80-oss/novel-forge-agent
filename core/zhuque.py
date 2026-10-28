"""腾讯云 EdgeOne 朱雀文本检测，兼容通用 HTTP 适配器与手动记录。

官方协议：https://cloud.tencent.com/document/product/1552/135974
Makers 内置模型：https://cloud.tencent.com/document/product/1552/137539
"""
from __future__ import annotations

import json
import asyncio
import math
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit, quote

import httpx

from . import config

MAKERS_ENDPOINT = "https://ai-gateway.edgeone.link/v1/providers/zhuque-text/classify"


def _get_path(data: Any, path: str) -> Any:
    cur = data
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except Exception:
                return None
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _normalize(value: Any, scale: float) -> float | None:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    scale = float(scale or 100)
    if scale <= 1.5 and num <= 1.5:
        num = num * 100
    elif scale not in (0, 100):
        num = num / scale * 100
    return max(0.0, min(100.0, round(num, 1)))


def validate_settings(cfg: dict) -> str:
    adapter = cfg.get("adapter", "generic")
    if adapter not in ("generic", "tencent", "makers"):
        return "未识别的检测接入方式，请在设置中重新选择。"
    endpoint = MAKERS_ENDPOINT if adapter == "makers" else str(cfg.get("endpoint") or "").strip()
    if not endpoint:
        return "没有配置检测地址；腾讯云官方接入请填写 EdgeOne 网关域名。"
    try:
        parsed = urlsplit(endpoint)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("https", "http") or not host or parsed.username or parsed.password:
            return "检测地址格式不正确，请填写带 https:// 的网关或完整接口地址。"
    except ValueError:
        return "检测地址格式不正确。"
    if any(host == h or host.endswith('.' + h) for h in ('example.com', 'example.org', 'example.net')) or '<' in endpoint or 'your-gateway' in endpoint:
        return "当前是示例地址，不是真实检测服务；请填写腾讯云分配的网关域名和 API Key。"
    if adapter in ("tencent", "makers"):
        if adapter == "tencent" and cfg.get("work_mode", "async") not in ("sync", "async"):
            return "朱雀调用方式应为同步或异步。"
        if parsed.scheme != "https" or parsed.query or parsed.fragment:
            return "腾讯云网关请使用 HTTPS 地址，不要附带查询参数或网页锚点。"
        if parsed.path.rstrip('/') not in ('', '/v1', '/v1/providers/zhuque-text', '/v1/providers/zhuque-text/classify'):
            return "请填写网关根地址，或以 /v1/providers/zhuque-text/classify 结尾的文本检测地址。"
        if not isinstance(cfg.get("api_key", ""), str):
            return "API Key 格式不正确，请重新粘贴网关密钥。"
        auth = str(cfg.get("api_key") or "").strip()
        if not auth or auth.lower() in ('xxx', '<api_key>', 'your-api-key', 'sk-xxx'):
            return "请填写 Makers Models API Key 或对应的网关密钥（不是写作模型 Key，也不是腾讯云 SecretId）。"
    try:
        timeout = float(cfg.get("timeout", 60))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        return "检测超时必须为正数。"
    return ""


def _failed(error: str) -> dict:
    return {"ok": False, "score": None, "error": error, "manual": False}


def _http_error(status: int) -> str:
    tips = {401: "API Key 错误或失效，请核对网关密钥。", 403: "网关未获准调用朱雀，请确认已开通 zhuque-text 权限。",
            404: "地址或任务不存在，请核对网关域名及接口路径。", 429: "检测接口限流，请稍后重试。"}
    return f"检测接口 HTTP {status}：" + tips.get(status, "上游服务异常，请稍后重试。" if status >= 500 else "请求不符合接口要求，请检查配置。")


async def _detect_tencent(text: str, cfg: dict) -> dict:
    makers = cfg.get("adapter") == "makers"
    parsed = urlsplit(MAKERS_ENDPOINT if makers else cfg["endpoint"].strip())
    base = urlunsplit((parsed.scheme, parsed.netloc, '/v1/providers/zhuque-text', '', ''))
    headers = {"Authorization": "Bearer " + cfg["api_key"].strip(), "Content-Type": "application/json"}
    # Makers 文档只声明同步接口，异步仅用于专属网关协议。
    asynchronous = not makers and cfg.get("work_mode", "async") == "async"
    if asynchronous:
        headers["Work-Mode"] = "async"
    timeout = float(cfg.get("timeout") or 60)
    deadline = time.monotonic() + timeout
    local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
    async with httpx.AsyncClient(timeout=timeout, trust_env=not local) as client:
        # 不自动重提任务：官方说明重试提交可能重复计费。
        response = await client.post(base + '/classify', headers=headers, json={"text": text, "is_merge": True})
        if response.status_code >= 400:
            return _failed(_http_error(response.status_code))
        data = response.json()
        if not isinstance(data, dict):
            return _failed("朱雀返回格式错误：应为 JSON 对象。")
        if asynchronous:
            task_id = data.get("TaskId")
            if not isinstance(task_id, str) or not task_id:
                return _failed("朱雀未返回检测任务编号 TaskId，请检查网关权限和调用模式。")
            query_headers = {"Authorization": headers["Authorization"], "Work-Infer-Action": "query"}
            query_url = base + '/query/' + quote(task_id, safe='')
            retries = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return _failed("朱雀检测等待超时，可在设置中增加超时；正文已保留。")
                response = await client.get(query_url, headers=query_headers, timeout=min(30, remaining))
                if response.status_code in (429, 500, 502, 503, 504) and retries < 2:
                    retries += 1
                    await asyncio.sleep(min(2 ** retries, max(0, deadline - time.monotonic())))
                    continue
                if response.status_code >= 400:
                    return _failed(_http_error(response.status_code))
                data = response.json()
                if not isinstance(data, dict):
                    return _failed("朱雀查询返回格式错误。")
                state = data.get("Status")
                if state == "SUCCEEDED":
                    output = data.get("Output")
                    data = json.loads(output) if isinstance(output, str) else output
                    break
                if state == "FAILED":
                    return _failed("朱雀检测任务失败，请在 EdgeOne 控制台查看原因及网关权限。")
                if state not in ("QUEUED", "PROCESSING"):
                    return _failed("朱雀返回未识别的任务状态，未记录分值。")
                await asyncio.sleep(min(1, max(0, deadline - time.monotonic())))
        if not isinstance(data, dict) or data.get("status") != "success":
            return _failed("朱雀推理未成功，未记录分值，请检查网关服务。")
        # 官方定义：整体疑似 AI 内容占比，不能误用某个分段或置信度当成占比。
        try:
            ratio = data["ratio_confidence"]
            if isinstance(ratio, bool):
                raise ValueError()
            ratio = float(ratio)
            if not math.isfinite(ratio) or not 0 <= ratio <= 1:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            return _failed("朱雀结果缺少合法的 ratio_confidence（0–1），未记录分值。")
        score = round(ratio * 100, 1)
        # 只保留判定元信息，不返回请求头、密钥或分段原文。
        diagnostics = {"status": data.get("status"), "ratio_confidence": ratio,
                       "softmax_confidence": data.get("softmax_confidence"),
                       "labels_ratio": data.get("labels_ratio", {}),
                       "segments": [{k: segment.get(k) for k in ("label", "conf", "position")}
                                    for segment in (data.get("segment_labels") or []) if isinstance(segment, dict)]}
        return {"ok": True, "score": score, "manual": False, "pass": score <= float(cfg.get("pass_threshold", 20)),
                "metric": "ratio_confidence", "labels_ratio": data.get("labels_ratio", {}),
                "diagnostics": diagnostics}


async def detect(text: str, settings: dict | None = None) -> dict:
    """返回 {ok, score, raw, error, manual}. score 已归一到 0-100。"""
    cfg = settings if settings is not None else config.load().get("zhuque", {})
    if not cfg.get("enabled"):
        return {"ok": False, "score": None, "error": "朱雀检测未启用", "manual": False}
    if cfg.get("mode") == "manual":
        return {"ok": False, "score": None, "error": "手动模式：请在章节页粘贴检测结果", "manual": True}
    error = validate_settings(cfg)
    if error:
        return _failed(error)
    if not isinstance(text, str) or not text.strip():
        return _failed("待检测正文为空。")
    if cfg.get("adapter") in ("tencent", "makers"):
        try:
            return await _detect_tencent(text, cfg)
        except httpx.TimeoutException:
            return _failed("朱雀检测请求超时，请增加超时设置；正文已保留。")
        except httpx.RequestError:
            return _failed("无法连接朱雀网关，请检查域名、网络和代理设置；正文已保留。")
        except (ValueError, TypeError):
            return _failed("朱雀返回了无法解析的数据，未记录分值。")
    endpoint = cfg["endpoint"].strip()

    template = cfg.get("body_template") or '{"text": "{{text}}"}'
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    body_text = template.replace("{{text}}", escaped).replace(
        "{{text_json}}", json.dumps(text, ensure_ascii=False)
    )
    try:
        body = json.loads(body_text)
    except Exception:
        body = {"text": text}

    headers = {"Content-Type": "application/json"}
    headers.update(cfg.get("headers") or {})
    method = (cfg.get("method") or "POST").upper()
    timeout = float(cfg.get("timeout") or 60)

    try:
        local = any(h in endpoint for h in ("127.0.0.1", "localhost", "0.0.0.0"))
        async with httpx.AsyncClient(timeout=timeout, trust_env=not local) as client:
            if method == "GET":
                resp = await client.get(endpoint, headers=headers,
                                        params={"text": text[:4000]})
            else:
                resp = await client.request(method, endpoint, headers=headers, json=body)
        if resp.status_code >= 400:
            return _failed(_http_error(resp.status_code))
        raw_text = resp.text
        try:
            data = resp.json()
        except Exception:
            data = None
        score = None
        if data is not None:
            score = _normalize(_get_path(data, cfg.get("score_path") or "score"),
                               cfg.get("score_scale") or 100)
        if score is None:
            m = re.search(r'"?(?:score|rate|aigc|概率|疑似)[^0-9]{0,12}([0-9]+(?:\.[0-9]+)?)', raw_text, re.I)
            if m:
                score = _normalize(m.group(1), cfg.get("score_scale") or 100)
        if score is None:
            return {"ok": False, "score": None,
                    "error": f"没能从返回里解析出分值，请检查 score_path。返回：{raw_text[:300]}",
                    "raw": raw_text, "manual": False}
        return {"ok": True, "score": score, "raw": raw_text[:2000], "manual": False,
                "pass": score <= float(cfg.get("pass_threshold", 20))}
    except httpx.TimeoutException:
        return _failed("检测请求超时，请检查服务或增加超时设置。")
    except httpx.RequestError:
        return _failed("无法连接检测服务，请检查实际接口地址、网络和代理设置。")
    except (ValueError, TypeError):
        return _failed("检测配置或返回数据无法解析，请检查请求头、模板和分值路径。")


def settings() -> dict:
    return config.load().get("zhuque", {})
