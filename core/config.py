"""全局配置：模型供应商、分阶段模型、余额预警、朱雀适配器、写作参数。"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG: dict = {
    # 供应商完全自定义：协议只分 openai（含国模/中转）和 anthropic
    "providers": [],
    # 每个环节用哪个模型，格式 "provider_id::model_name"
    "stage_models": {
        "analyze": "",   # 拆书细纲、六维分析
        "outline": "",   # 原创大纲、创作宪法
        "write": "",     # 正文生成
        "check": "",     # 承接卡、偏离检测（留空则用 write 的模型）
        "polish": "",    # 去 AIGC / 全书优化
    },
    "balance": {
        "currency": "元",
        "amount": 0.0,
        "warning_threshold": 3.0,
        "prices": {},  # {"模型名": {"input": 2.0, "output": 8.0}} 单位：元/百万 token
    },
    "zhuque": {
        "enabled": False,
        "mode": "api",  # api | manual
        "adapter": "generic",  # generic | makers | tencent（专属网关）
        "api_key": "",
        "work_mode": "async",  # 腾讯云：async | sync
        "endpoint": "",
        "method": "POST",
        "headers": {},
        "body_template": "{\"text\": \"{{text}}\"}",
        "score_path": "data.score",
        "score_scale": 100,  # 返回值满分，如 1 表示 0-1 小数
        "pass_threshold": 20,
        "max_rounds": 2,
        "timeout": 60,
    },
    "writing": {
        "batch_size": 5,
        "words_per_chapter": 2600,
        "context_tail_chars": 1800,
        "auto_continue": False,
    },
    "analysis": {
        "detail_parallelism": 1,
        "archive_interval": 2,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _normalize(cfg: dict) -> dict:
    """兼容旧配置 / 前端误传：模型列表统一成字符串数组。"""
    for p in cfg.get("providers") or []:
        models = p.get("models")
        if isinstance(models, str):
            models = re.split(r"[,，\s]+", models)
        if isinstance(models, list):
            p["models"] = [str(m).strip() for m in models if str(m).strip()]
        elif models is None:
            p["models"] = []
        if not isinstance(p.get("extra_headers"), dict):
            p["extra_headers"] = {}
    return cfg


def load() -> dict:
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            user = {}
    else:
        user = {}
    return _normalize(_deep_merge(DEFAULT_CONFIG, user))


def save(cfg: dict) -> dict:
    cfg = _normalize(_deep_merge(DEFAULT_CONFIG, cfg))
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return cfg


def get_provider(provider_id: str) -> dict | None:
    for p in load().get("providers", []):
        if p.get("id") == provider_id:
            return p
    return None


def resolve_stage(stage: str) -> tuple[dict, str]:
    """返回 (provider, model)。找不到时抛出 ValueError。"""
    cfg = load()
    stages = cfg.get("stage_models", {})
    value = stages.get(stage) or ""
    if not value and stage in ("check", "polish"):
        value = stages.get("write") or ""
    if not value:
        raise ValueError(f"还没有为「{stage}」环节选择模型，请到设置页配置。")
    if "::" not in value:
        raise ValueError(f"模型配置格式错误：{value}")
    pid, model = value.split("::", 1)
    provider = get_provider(pid)
    if not provider:
        raise ValueError(f"找不到模型供应商：{pid}")
    if not provider.get("api_key") and provider.get("type") != "mock":
        raise ValueError(f"供应商「{provider.get('name', pid)}」还没有填写 API Key。")
    return provider, model


def provider_ready(p: dict) -> bool:
    return bool(p.get("api_key")) or p.get("type") == "mock"
