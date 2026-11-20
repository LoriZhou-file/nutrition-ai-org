"""知识检索（雏形）。

读取 knowledge/*/extracted 下的知识单元，用字符二元组重合度做简单检索。
雏形阶段所有知识均为“待审核”，仅用于内部演示。
"""
from __future__ import annotations

import pathlib
import re
from functools import lru_cache

ROOT = pathlib.Path(__file__).resolve().parent.parent / "knowledge"
DEFAULT_IDS = ["KB02-002", "KB00-004"]


def _field(text: str, name: str) -> str:
    m = re.search(r"\|\s*\*\*" + re.escape(name) + r"\*\*\s*\|\s*(.*?)\s*\|\s*$", text, re.M)
    return m.group(1).strip() if m else ""


@lru_cache(maxsize=1)
def load_units() -> list[dict]:
    units = []
    for p in sorted(ROOT.glob("KB-0[0-6]*/extracted/*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        text = p.read_text(encoding="utf-8")
        uid = _field(text, "知识编号") or p.name.split("_")[0]
        units.append(
            {
                "id": uid,
                "topic": _field(text, "知识主题"),
                "core": _field(text, "核心知识"),
                "limits": _field(text, "适用限制"),
                "risk": _field(text, "风险提示"),
                "status": _field(text, "审核状态") or "待审核",
                "body": text.split("## 核心知识正文", 1)[-1].split("## 变更记录", 1)[0].strip()[:700],
                "path": str(p.relative_to(ROOT.parent)),
            }
        )
    return units


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i : i + 2] for i in range(len(s) - 1)}


def retrieve(query: str, k: int = 4) -> list[dict]:
    q = _bigrams(query)
    scored = []
    for u in load_units():
        doc = _bigrams(u["topic"] + u["core"] + u["body"])
        score = len(q & doc)
        if score:
            scored.append((score, u))
    scored.sort(key=lambda x: -x[0])
    picked = [u for _, u in scored[:k]]
    if not picked:
        picked = [u for u in load_units() if u["id"] in DEFAULT_IDS]
    return picked
