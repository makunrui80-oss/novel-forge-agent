"""统一大模型调用层：Anthropic / OpenAI / OpenAI 兼容（DeepSeek、通义、Kimi、豆包等）。"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from . import config, db


class LLMError(RuntimeError):
    """模型调用错误。

    retryable 用来区分“服务不可用/鉴权失败”和“模型没有给最终答案”两类错误。
    后者可以安全地用更明确的提示词重试，前者继续重试通常只会浪费额度。
    """

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _text_from_parts(value: Any) -> str:
    """兼容 OpenAI 兼容接口返回的字符串、内容块和 Responses 风格结构。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(item.get("text") or item.get("content") or "")
        return "".join(str(x) for x in parts if x)
    if isinstance(value, dict):
        return str(value.get("text") or value.get("content") or "")
    return ""


def _extract_response_text(data: dict, ptype: str) -> str:
    """从不同供应商的响应格式中提取最终答案。"""
    if ptype == "anthropic":
        return _text_from_parts(data.get("content") or data.get("output") or "")

    # OpenAI Responses API / 部分中转站会直接提供 output_text。
    direct = _text_from_parts(data.get("output_text"))
    if direct.strip():
        return direct
    output = data.get("output")
    if output:
        text = _text_from_parts(output)
        if text.strip():
            return text

    choices = data.get("choices") or []
    if not choices:
        return ""
    choice = choices[0] or {}
    message = choice.get("message") or {}
    text = _text_from_parts(message.get("content"))
    if text.strip():
        return text
    # 一些推理模型把最终答案放在 text 字段，或用 function/tool call 返回 JSON。
    text = _text_from_parts(message.get("text") or choice.get("text"))
    if text.strip():
        return text
    text = _text_from_parts((choice.get("delta") or {}).get("content"))
    if text.strip():
        return text
    calls = message.get("tool_calls") or choice.get("tool_calls") or []
    if calls:
        args = (calls[0].get("function") or {}).get("arguments")
        if isinstance(args, str) and args.strip():
            return args
        if isinstance(args, dict):
            return json.dumps(args, ensure_ascii=False)
    return ""


def _response_preview(data: Any) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)[:600]
    except Exception:
        return str(data)[:600]


def _parse_sse(text: str, ptype: str) -> dict | None:
    """尽量兼容忽略 stream=false 的中转站，把 SSE 拼成普通响应。"""
    if "data:" not in text:
        return None
    chunks = []
    usage = {}
    finish = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            continue
        try:
            item = json.loads(raw)
        except Exception:
            continue
        usage.update(item.get("usage") or {})
        if ptype == "anthropic":
            delta = item.get("delta") or {}
            finish = delta.get("stop_reason") or finish
            if delta.get("text"):
                chunks.append(delta["text"])
        else:
            choices = item.get("choices") or []
            if choices:
                choice = choices[0] or {}
                finish = choice.get("finish_reason") or finish
                chunks.append(_text_from_parts((choice.get("delta") or {}).get("content")))
                chunks.append(_text_from_parts((choice.get("message") or {}).get("content")))
    text_out = "".join(chunks)
    return {"content": [{"type": "text", "text": text_out}], "usage": usage, "stop_reason": finish} if ptype == "anthropic" \
        else {"choices": [{"message": {"content": text_out}, "finish_reason": finish}], "usage": usage}


def _is_local(url: str) -> bool:
    return any(h in url for h in ("127.0.0.1", "localhost", "0.0.0.0", "[::1]"))


def _endpoint(provider: dict) -> str:
    base = (provider.get("base_url") or "").rstrip("/")
    ptype = provider.get("type", "openai")
    if ptype == "anthropic":
        if not base:
            base = "https://api.anthropic.com"
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return base + "/messages"
        return base + "/v1/messages"
    if not base:
        base = "https://api.openai.com/v1"
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _price_for(model: str) -> dict:
    prices = config.load().get("balance", {}).get("prices", {})
    p = prices.get(model) or {}
    try:
        return {"input": float(p.get("input", 0) or 0), "output": float(p.get("output", 0) or 0)}
    except Exception:
        return {"input": 0.0, "output": 0.0}


def _record_usage(project_id: int | None, stage: str, provider: dict, model: str,
                  usage: dict) -> float:
    pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    price = _price_for(model)
    cost = pt / 1_000_000 * price["input"] + ct / 1_000_000 * price["output"]
    db.execute(
        "INSERT INTO usage_log(ts, project_id, stage, provider, model, prompt_tokens, completion_tokens, cost) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (db.now(), project_id, stage, provider.get("id", ""), model, pt, ct, cost),
    )
    if cost > 0:
        cfg = config.load()
        bal = cfg.get("balance", {})
        if float(bal.get("amount") or 0) > 0:
            bal["amount"] = round(float(bal["amount"]) - cost, 4)
            cfg["balance"] = bal
            config.save(cfg)
    return cost


async def chat(stage: str, system: str, user: str, *, project_id: int | None = None,
               max_tokens: int = 4096, temperature: float = 0.8,
               retries: int = 3, provider: dict | None = None,
               model: str | None = None, timeout: float | None = 600.0,
               allow_truncated: bool = False) -> str:
    if not provider or not model:
        provider, model = config.resolve_stage(stage)
    ptype = provider.get("type", "openai")
    url = _endpoint(provider)
    timeout_cfg = httpx.Timeout(timeout, connect=30.0) if timeout is not None else None

    if ptype == "anthropic":
        headers = {
            "x-api-key": provider.get("api_key", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        headers.update(provider.get("extra_headers") or {})
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": max(0.0, min(1.0, temperature)),
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
    else:
        headers = {
            "Authorization": f"Bearer {provider.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        headers.update(provider.get("extra_headers") or {})
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": max(0.0, min(2.0, temperature)),
            "max_tokens": max_tokens,
            # 统一要求非流式响应；许多自建中转默认开启流式，直接 json() 会被误判为空。
            "stream": bool(provider.get("stream") is True),
        }

    last_err = ""
    attempts = max(1, int(retries))
    retry_user = user
    token_budget = max_tokens
    disable_thinking = False
    for attempt in range(attempts):
        try:
            request_body = dict(body)
            if retry_user != user:
                if ptype == "anthropic":
                    request_body["messages"] = [{"role": "user", "content": retry_user}]
                else:
                    request_body["messages"] = [
                        {"role": "system", "content": system},
                        {"role": "user", "content": retry_user},
                    ]
            request_body["max_tokens"] = token_budget
            if disable_thinking:
                # 只对已确认耗尽推理预算的 DeepSeek 兼容请求尝试关闭思考。
                request_body["thinking"] = {"type": "disabled"}
            async with httpx.AsyncClient(timeout=timeout_cfg, trust_env=not _is_local(url)) as client:
                resp = await client.post(url, headers=headers, json=request_body)
            if resp.status_code >= 400:
                snippet = resp.text[:600]
                if resp.status_code in (400, 422) and disable_thinking and attempt < attempts - 1:
                    disable_thinking = False
                    continue  # 中转不支持 thinking 参数时，保留增加后的预算重试。
                if resp.status_code in (401, 403):
                    raise LLMError(f"鉴权失败（{resp.status_code}），请检查 API Key：{snippet}")
                if resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504, 529) and attempt < attempts - 1:
                    last_err = f"HTTP {resp.status_code}: {snippet}"
                    await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))
                    continue
                raise LLMError(f"模型接口错误（{resp.status_code}）：{snippet}")
            try:
                data = resp.json()
            except Exception as exc:
                data = _parse_sse(resp.text, ptype)
                if data is not None:
                    # SSE 已拼接成普通响应，继续走统一提取逻辑。
                    pass
                else:
                    last_err = f"接口返回不是 JSON：{resp.text[:300]}"
                    if attempt < attempts - 1:
                        retry_user = user + "\n\n上一次接口没有返回可解析的最终答案，请只输出本任务要求的正文或 JSON，不要解释。"
                        await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))
                        continue
                    raise LLMError(last_err) from exc
            usage = data.get("usage") or {}
            # 记录空回复的 token 消耗，避免把失败请求误算成免费调用。
            _record_usage(project_id, stage, provider, model, usage)
            text = _extract_response_text(data, ptype)
            choices = data.get("choices") or []
            finish = choices[0].get("finish_reason") if choices else data.get("stop_reason")
            truncated = finish in ("length", "max_tokens")
            if truncated and text.strip() and allow_truncated:
                return text.strip()  # 拆书调用会回收已完整输出的摘要。
            if truncated or not text.strip():
                token_budget = min(32768, max(token_budget * 2, token_budget + 2000))
                if truncated and ptype != "anthropic" and "deepseek" in model.lower():
                    disable_thinking = True
            if not text.strip() or truncated:
                # 错误仅记录诊断元信息，不把 reasoning_content 混入界面或最终文本。
                detail = f"finish_reason={finish!r}; 输出预算={request_body['max_tokens']}; 返回正文={len(text)} 字符"
                last_err = ("模型输出被截断" if text.strip() else "模型没有返回最终文本") + f"（{detail}）"
                if attempt < attempts - 1:
                    retry_user = user + (
                        "\n\n上一次请求没有返回可用的最终答案。请直接给出完整最终内容，"
                        "不要只输出思考过程、工具调用或空内容。"
                    )
                    await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))
                    continue
                raise LLMError(last_err)
            return text.strip()
        except LLMError as exc:
            if not exc.retryable or attempt >= attempts - 1:
                raise
            last_err = str(exc)
            await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))
                continue
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < attempts - 1:
                await asyncio.sleep(min(8, 1.5 * (2 ** attempt)))
                continue
    raise LLMError(f"调用失败：{last_err}")


async def test_provider(provider_id: str, model: str) -> str:
    provider = config.get_provider(provider_id)
    if not provider:
        raise LLMError("供应商不存在")
    return await chat("test", "你是连通性测试助手。", "只回复四个字：连接成功",
                      max_tokens=32, temperature=0, retries=1,
                      provider=provider, model=model, timeout=30.0)
