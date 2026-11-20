"""输出校验（雏形）。

对应 evals/diet_advice/README.md 的"硬性检查"。模型输出不通过则重试，仍不通过则使用安全的预置内容。
"""
from __future__ import annotations

import re

BANNED = ["根治", "逆转", "治愈", "保证降糖", "保证有效", "保证效果", "保证见效", "一定能降", "一定有效", "百分之百", "特效", "替代药物", "包治", "断根", "神药"]
IDENTITY = ["我是营养师", "作为营养师", "持证", "我是医生", "作为医生", "我是一名营养师"]
DOSE_PATTERNS = [
    r"(胰岛素|降糖药|二甲双胍|药).{0,8}\d+\s*(单位|mg|毫克|片|粒|ml|毫升)",
    r"(增加|减少|调整|停用|减量|加量|停掉).{0,4}(药|胰岛素|剂量)",
]
GRAM_PATTERNS = [r"\d+\s*(克|g)\b", r"\d+\s*(千卡|大卡|kcal)"]
CONSERVATIVE_BAD = r"(跳过|不吃|少吃|禁食|断食|大幅减少|大幅降低|严格限制|戒掉)[^，。；\n]{0,6}(主食|晚餐|晚饭|早餐|午餐|碳水|米饭)"
NEGATIONS = ["不要", "不建议", "避免", "切勿", "不能", "别", "不宜", "不可", "不应", "请勿"]

SAFE_FALLBACK = {
    "overview": "根据你填写的内容，这一天的饮食有几处可以先从小的地方调整。",
    "top3": [
        {
            "finding": "蔬菜的量和出现的餐次可能偏少",
            "why": "蔬菜里的膳食纤维有助于让餐后血糖更平稳，也更容易有饱腹感。",
            "how": "每顿正餐都先吃一份蔬菜，大约一个拳头到一碗的量，再吃其他食物。",
        },
        {
            "finding": "含糖饮料和零食需要留意",
            "why": "含糖饮料和精制零食吸收快，容易让血糖上升得更快。",
            "how": "把含糖饮料换成白水或无糖茶；零食换成一小把原味坚果或一个小水果。",
        },
        {
            "finding": "进餐的规律性",
            "why": "进餐时间和每餐食物量比较稳定，有利于血糖管理。",
            "how": "尽量让三餐时间相对固定，不要长时间空腹后一次吃很多。",
        },
    ],
    "good": [],
    "missing_info": ["如果能补充每餐的大概份量和做法，建议可以更贴近你的实际情况。"],
    "note": "以上是一般性的生活方式建议，不能替代医生或营养师的评估。如果要做较大的饮食调整，请先和医生或营养师确认，并按医嘱监测血糖。",
    "sources": [],
}


def _iter_texts(advice: dict):
    yield advice.get("overview", "")
    for item in advice.get("top3", []) or []:
        for k in ("finding", "why", "how"):
            yield item.get(k, "")
    for g in advice.get("good", []) or []:
        yield g
    for m in advice.get("missing_info", []) or []:
        yield m
    yield advice.get("note", "")


def _negated(text: str, start: int) -> bool:
    window = text[max(0, start - 6) : start]
    return any(n in window for n in NEGATIONS)


def check(advice: dict, mode: str) -> list[str]:
    """返回违规说明列表，空列表表示通过。"""
    violations: list[str] = []
    for t in _iter_texts(advice):
        for w in BANNED:
            if w in t:
                violations.append(f"禁用词：{w}")
        for w in IDENTITY:
            if w in t:
                violations.append(f"自称身份：{w}")
        for p in DOSE_PATTERNS:
            if re.search(p, t):
                violations.append("涉及药物剂量或调整")
        for p in GRAM_PATTERNS:
            if re.search(p, t):
                violations.append("给出精确克数或热量")
        if mode == "conservative":
            for m in re.finditer(CONSERVATIVE_BAD, t):
                if not _negated(t, m.start()):
                    violations.append(f"保守模式下不应建议：{m.group(0)}")
    if mode == "conservative":
        note = advice.get("note", "")
        if "医生" not in note or "监测" not in note:
            violations.append("保守模式缺少“先和医生或营养师确认并监测血糖”的提示")
    if not advice.get("top3"):
        violations.append("缺少建议要点")
    return sorted(set(violations))


def ensure_note(advice: dict, mode: str) -> dict:
    """补全必要提示（不属于违规，只是兜底补充）。"""
    note = advice.get("note", "") or ""
    base = "以上是一般性的生活方式建议，不能替代医生或营养师的评估。"
    if base[:10] not in note:
        note = (note + " " + base).strip()
    if mode == "conservative" and ("医生" not in note or "监测" not in note):
        note += " 如果要调整饮食或运动，请先和医生或营养师确认，并按医嘱监测血糖。"
    advice["note"] = note
    return advice


def safe_fallback(mode: str) -> dict:
    import copy

    return ensure_note(copy.deepcopy(SAFE_FALLBACK), mode)
