"""规则引擎（雏形）。

先于大模型执行。读取 KB-07 的规则框架（id、level、action、话术），
关键词使用 demo_keywords.yaml（演示用，非审定）。
"""
from __future__ import annotations

import pathlib
from functools import lru_cache

import yaml

BASE = pathlib.Path(__file__).resolve().parent
RULES_PATH = BASE.parent / "knowledge" / "KB-07_风险识别与医疗转介" / "rules" / "risk_rules.yaml"
KEYWORDS_PATH = BASE / "demo_keywords.yaml"


@lru_cache(maxsize=1)
def _rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _keywords() -> dict:
    return yaml.safe_load(KEYWORDS_PATH.read_text(encoding="utf-8"))


def rule_defs() -> dict[str, dict]:
    return {r["id"]: r for r in _rules()["rules"]}


def message(msg_id: str) -> str:
    return _rules()["messages"][msg_id]


def _hits(text: str, words: list[str]) -> list[str]:
    return [w for w in words if w in text]


def compute_mode(glycemic: dict) -> str:
    """用药或不确定是否用药 → 保守模式（比"仅胰岛素"更保守，待营养师确认）。"""
    drug = (glycemic or {}).get("uses_glucose_lowering_drug", "no")
    return "standard" if drug == "no" else "conservative"


def screen(screening: dict) -> dict:
    """S1 排除筛查。任一命中 → referred。"""
    s = screening or {}
    excluded = (
        s.get("adult") is not True
        or s.get("pregnant_or_lactating") is True
        or s.get("type1_diabetes") is True
        or s.get("severe_kidney_or_eating_disorder") is True
    )
    if excluded:
        return {"referred": True, "rule_id": "R-EXCL", "message": message("MSG-EXCL")}
    return {"referred": False, "rule_id": None, "message": None}


def scan_text(text: str, mode: str) -> dict:
    """对用户自由文本做规则扫描。

    返回：
      flags: 命中的规则 id 列表
      stop: 是否终止营养建议（红）
      stop_messages: 终止时的固定话术
      notices: 不终止但需附加的提示 [{rule_id, message}]
      forbid_topics: 需要回避的话题（如药量调整）
      risk_level: green / yellow / red
    """
    kw = _keywords()
    defs = rule_defs()
    medicated = mode == "conservative"
    flags: list[str] = []

    for rid in ("R-EMG", "R-ED", "R-MED", "R-EXTREME", "R-DX", "R-RAPID"):
        if _hits(text, kw[rid]["any"]):
            flags.append(rid)

    hypo = kw["R-HYPO"]
    strong = _hits(text, hypo["strong"])
    weak = _hits(text, hypo["weak"])
    if (len(strong) + len(weak) >= hypo["min_total"]) or (
        medicated and len(strong) >= hypo["min_strong_if_medicated"]
    ):
        flags.append("R-HYPO")

    groups = kw["R-HYPER"]["all_groups"]
    if all(_hits(text, g) for g in groups):
        flags.append("R-HYPER")

    stop_ids = [f for f in flags if defs[f]["action"].get("stop_nutrition_advice")]
    notice_ids = [f for f in flags if f not in stop_ids]

    risk_level = "red" if stop_ids else ("yellow" if flags else "green")
    return {
        "flags": flags,
        "stop": bool(stop_ids),
        "stop_messages": [message(defs[f]["action"]["message_id"]) for f in stop_ids],
        "notices": [
            {"rule_id": f, "message": message(defs[f]["action"]["message_id"])} for f in notice_ids
        ],
        "forbid_topics": [defs[f]["action"].get("refuse_topic") for f in notice_ids if defs[f]["action"].get("refuse_topic")],
        "risk_level": risk_level,
    }
