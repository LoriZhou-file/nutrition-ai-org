"""大模型适配层。

- 有 DEEPSEEK_API_KEY（或 LLM_API_KEY）→ 调用 DeepSeek（OpenAI 兼容接口）。
- 没有 → 演示模式（mock）：用规则生成示例输出，只用于看流程，不代表真实模型水平。
可通过 LLM_BASE_URL / LLM_MODEL 切换到其他兼容接口，不改业务代码。
"""
from __future__ import annotations

import json
import os
import pathlib
import re

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / "prototype" / ".env")


def settings() -> dict:
    key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("LLM_API_KEY") or ""
    return {
        "api_key": key,
        "base_url": os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
        "model": os.getenv("LLM_MODEL", "deepseek-chat"),
    }


def engine_name() -> str:
    s = settings()
    if os.getenv("FORCE_MOCK") == "1" or not s["api_key"]:
        return "mock"
    return s["model"]


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        text = m.group(0)
    return json.loads(text)


def chat_json(system: str, user: str, kind: str, context: dict, temperature: float = 0.3) -> dict:
    """kind: clarify / advice。context 供演示模式使用。"""
    if engine_name() == "mock":
        from . import mock

        return mock.respond(kind, context)

    from openai import OpenAI

    s = settings()
    client = OpenAI(api_key=s["api_key"], base_url=s["base_url"], timeout=60)
    last_err: Exception | None = None
    for _ in range(2):
        try:
            resp = client.chat.completions.create(
                model=s["model"],
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            return _parse_json(resp.choices[0].message.content or "")
        except Exception as e:  # 解析失败或接口错误，重试一次
            last_err = e
    raise RuntimeError(f"模型调用失败：{last_err}")
