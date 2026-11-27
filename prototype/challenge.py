"""30 天控糖自我管理（雏形）。

- 任务与话术来自 knowledge/challenges/30day_glycemic.yaml（待营养师和医生审核）。
- 每周日（第 7、14、21、28 天）出一次生活小结，第 30 天出总结。
- 档案就是打卡记录本身：每次打卡补充行为、障碍和一个点选答案，不需要用户打字或填数值。
- 反馈以模板为主；只有用户写了备注时，才让模型回应一句，并过输出校验。
- 备注先过风险规则；出现红级信号后暂停挑战。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import uuid
from functools import lru_cache
from zoneinfo import ZoneInfo

import jsonschema
import yaml

from . import chat, guard, llm, prompts, rules

BASE = pathlib.Path(__file__).resolve().parent
DATA = BASE / "data"
CONTENT = BASE.parent / "knowledge" / "challenges" / "30day_glycemic.yaml"
SCHEMA = json.loads((BASE.parent / "schemas" / "user_profile.schema.json").read_text(encoding="utf-8"))
TZ = ZoneInfo("Asia/Shanghai")
DAYS = 30
WEEK_ENDS = (7, 14, 21, 28)
DRUG_VALUES = chat.DRUG_VALUES


class ChallengeError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=1)
def content() -> dict:
    return yaml.safe_load(CONTENT.read_text(encoding="utf-8"))


def _task(day: int) -> dict:
    return next(t for t in content()["tasks"] if t["day"] == day)


def _today() -> dt.date:
    return dt.datetime.now(TZ).date()


def demo_enabled() -> bool:
    return os.getenv("ALLOW_DEMO_ADVANCE", "1") == "1"


# ---------------------------------------------------------------- 存取
def _path(sid: str) -> pathlib.Path:
    try:
        uuid.UUID(sid)
    except (ValueError, AttributeError, TypeError):
        raise ChallengeError("无效的会话标识")
    return DATA / f"{sid}.json"


def load(sid: str) -> dict | None:
    p = _path(sid)
    if not p.exists():
        return None
    doc = json.loads(p.read_text(encoding="utf-8"))
    return doc if "challenge" in doc else None


def save(doc: dict) -> None:
    doc["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    try:
        jsonschema.validate(doc, SCHEMA)
    except jsonschema.ValidationError as e:
        raise ChallengeError(f"档案不符合数据结构：{e.message}", 500)
    DATA.mkdir(exist_ok=True)
    _path(doc["session_id"]).write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 开始
def start(payload: dict) -> dict:
    sid = payload.get("session_id")
    existing = load(sid)
    if existing:
        return view(existing)
    if payload.get("consent") is not True:
        raise ChallengeError("需要先同意健康信息说明")
    sc = {k: payload.get("screening", {}).get(k) for k in chat.SCREEN_KEYS}
    if any(v is None for v in sc.values()):
        raise ChallengeError("请完成全部安全确认")
    screening = {k: bool(v) for k, v in sc.items()}
    if screening["adult"] is not True:
        raise ChallengeError("本工具面向成年人。" + rules.message("MSG-EXCL"), 403)
    drug = payload.get("uses_glucose_lowering_drug")
    if drug not in DRUG_VALUES:
        raise ChallengeError("请选择是否在使用降糖药物")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    doc = {
        "session_id": sid,
        "source": payload.get("source") or "challenge",
        "consent": {"health_data": True, "interest_contact": False, "version": "demo-0.1", "timestamp": now},
        "screening": screening,
        "mode": "standard" if drug == "no" else "conservative",
        "basic": {},
        "glycemic": {"diagnosed": "unsure", "recent_indicators": [], "uses_glucose_lowering_drug": drug},
        "lifestyle": {},
        "concerns": [],
        "challenge": {"program": content()["id"], "version": content()["version"], "started_on": _today().isoformat(),
                      "demo_offset_days": 0, "blocked": False, "checkins": []},
        "risk_level": "green",
        "risk_flags": [],
        "advice_summary_id": None,
        "interest": {"coaching_interest": False, "notes": None},
        "user_confirmed": False,
        "status": "in_progress",
        "created_at": now,
        "updated_at": now,
    }
    save(doc)
    return view(doc)


# ---------------------------------------------------------------- 视图
def current_day(doc: dict) -> int:
    c = doc["challenge"]
    started = dt.date.fromisoformat(c["started_on"])
    return max(1, min(DAYS, (_today() - started).days + 1 + int(c.get("demo_offset_days", 0))))


def _checkin(doc: dict, day: int) -> dict | None:
    return next((c for c in doc["challenge"]["checkins"] if c["day"] == day), None)


def _task_view(doc: dict, day: int) -> dict:
    t = _task(day)
    q = t.get("question")
    v = {"day": day, "title": t["title"], "task": t["task"], "why": t["why"], "how": t["how"],
         "question": {"text": q["text"], "options": q["options"]} if q else None}
    if doc["mode"] == "conservative":
        note = t.get("note_hypo") or t.get("note_conservative")
        if note:
            v["note"] = note
    v["kind"] = t.get("kind")
    return v


def _stats(cks: list[dict]) -> dict:
    done = [k for k in cks if k["status"] == "done"]
    partial = [k for k in cks if k["status"] == "partial"]
    skipped = [k for k in cks if k["status"] == "skipped"]
    counts: dict[str, int] = {}
    for k in skipped:
        if k.get("barrier"):
            counts[k["barrier"]] = counts.get(k["barrier"], 0) + 1
    labels = {b["id"]: b["label"] for b in content()["barriers"]}
    top = labels[max(counts, key=counts.get)] if counts else None
    return {"done": len(done), "partial": len(partial), "skipped": len(skipped), "top_barrier": top}


def _by_kind(cks: list[dict]) -> dict[str, int]:
    """各类任务里“做到了或做了一部分”的天数。"""
    out: dict[str, int] = {}
    for k in cks:
        if k["status"] in ("done", "partial"):
            kind = _task(k["day"]).get("kind")
            if kind:
                out[kind] = out.get(kind, 0) + 1
    return out


def weekly(doc: dict) -> list[dict]:
    """已经走完的每一周的生活小结（第 7、14、21、28 天）。"""
    cur = current_day(doc)
    cks = doc["challenge"]["checkins"]
    kind_labels = content()["kind_labels"]
    out = []
    for w in content()["weeks"]:
        first, last = w["days"]
        if not (_checkin(doc, last) or cur > last):
            continue
        week_cks = [k for k in cks if first <= k["day"] <= last]
        st = _stats(week_cks)
        n = last - first + 1
        lines = [f"第 {w['week']} 周（第 {first} 到 {last} 天）：{n} 天里，你有 {st['done']} 天做到了，{st['partial']} 天做了一部分，{st['skipped']} 天没做到。"]
        by_kind = _by_kind(week_cks)
        walk, resist = by_kind.get("walk", 0), by_kind.get("resist", 0)
        parts = []
        if walk:
            parts.append(f"{kind_labels['walk']} {walk} 天")
        if resist:
            parts.append(f"{kind_labels['resist']} {resist} 天")
        if parts:
            lines.append("动起来的：" + "，".join(parts) + "。")
        easiest = next((_task(k["day"])["title"] for k in week_cks if k["status"] == "done" and _task(k["day"]).get("kind") in ("diet", "walk", "resist")), None)
        if easiest:
            lines.append(f"最顺手的是「{easiest}」。")
        if st["top_barrier"]:
            lines.append(f"最常遇到的阻碍是「{st['top_barrier']}」。")
        lines.append(w["next"] if w.get("next") else "下一周继续，一次只多做一点。")
        out.append({"week": w["week"], "title": w["title"], "days": [first, last], "lines": lines, **st})
    return out


def summary(doc: dict) -> dict | None:
    """第 30 天的 30 天总结。"""
    cks = doc["challenge"]["checkins"]
    if not _checkin(doc, DAYS):
        return None
    c = content()
    st = _stats(cks)
    keep = next((k["answer"] for k in cks if k["day"] == 24 and k.get("answer")), None)
    by_kind = _by_kind(cks)
    kind_labels = c["kind_labels"]
    lines = [f"30 天里，你有 {st['done']} 天做到了，{st['partial']} 天做了一部分，{st['skipped']} 天没做到。"]
    parts = [f"{kind_labels[k]} {by_kind[k]} 天" for k in ("walk", "resist") if by_kind.get(k)]
    if parts:
        lines.append("动起来的：" + "，".join(parts) + "。")
    if st["top_barrier"]:
        lines.append(f"最常遇到的阻碍是「{st['top_barrier']}」。")
    if keep:
        lines.append(f"你想继续坚持的是「{keep}」。")
    lines.append("能走完这 30 天，说明你有改变的意愿，这是最重要的。")
    return {"title": c["summary"]["title"], "lines": lines, **st, "keep": keep, "invite": c["summary"]["invite"]}


def view(doc: dict) -> dict:
    cur = current_day(doc)
    cks = doc["challenge"]["checkins"]
    days = [{"day": d, "title": _task(d)["title"], "status": (_checkin(doc, d) or {}).get("status"), "unlocked": d <= cur, "week": (d - 1) // 7 + 1}
            for d in range(1, DAYS + 1)]
    out = {
        "session_id": doc["session_id"], "mode": doc["mode"], "current_day": cur, "days": days,
        "blocked": bool(doc["challenge"].get("blocked")), "demo": demo_enabled(),
        "statuses": content()["statuses"], "barriers": content()["barriers"], "footer": content()["common_footer"],
        "days_total": DAYS,
        "interest": doc["interest"], "confirmed": doc["user_confirmed"],
    }
    if out["blocked"]:
        defs = rules.rule_defs()
        stop = [f for f in doc["risk_flags"] if defs[f]["action"].get("stop_nutrition_advice")]
        out["stop_messages"] = [rules.message(defs[f]["action"]["message_id"]) for f in stop[:1]]
        return out
    out["today"] = _task_view(doc, cur)
    out["tasks"] = {d: _task_view(doc, d) for d in range(1, cur + 1)}
    out["today_checkin"] = _checkin(doc, cur)
    out["checkins"] = {c["day"]: c for c in cks}
    out["weekly"] = weekly(doc)
    out["summary"] = summary(doc)
    return out


# ---------------------------------------------------------------- 打卡
def _require(sid: str) -> dict:
    doc = load(sid)
    if not doc:
        raise ChallengeError("还没有开始挑战", 404)
    return doc


def _llm_reply(doc: dict, day: int, status: str, note: str) -> str | None:
    """用户写了备注时，让模型回应一句。不通过校验则不显示，不影响主流程。"""
    mode = doc["mode"]
    t = _task(day)
    try:
        out = llm.chat_json(
            prompts.feedback_system(mode),
            f"今天的任务：{t['task']}\n用户的完成情况：{content()['statuses'][status]}\n用户写的一句话：{note}",
            "feedback", {"note": note, "mode": mode},
        )
    except RuntimeError:
        return None
    reply = str(out.get("reply") or "").strip()
    if not reply or len(reply) > 120 or guard.check_text(reply, mode):
        return None
    return reply


def checkin(sid: str, payload: dict) -> dict:
    doc = _require(sid)
    ch = doc["challenge"]
    if ch.get("blocked"):
        raise ChallengeError("本次挑战已暂停", 409)
    cur = current_day(doc)
    day = payload.get("day")
    if not isinstance(day, int) or not 1 <= day <= cur:
        raise ChallengeError("这一天还没有解锁")
    status = payload.get("status")
    c = content()
    if status not in c["statuses"]:
        raise ChallengeError("无效的完成情况")
    barrier = payload.get("barrier") if status == "skipped" else None
    if barrier is not None and barrier not in {b["id"] for b in c["barriers"]}:
        raise ChallengeError("无效的选项")
    t = _task(day)
    q = t.get("question")
    answer = payload.get("answer")
    if answer is not None and (not q or answer not in q["options"]):
        raise ChallengeError("无效的选项")
    if status == "skipped":
        answer = None
    note = (payload.get("note") or "").strip()[:200] or None

    result: dict = {"notices": []}
    if note:
        scan = rules.scan_text(note, doc["mode"])
        chat_flags = [f for f in scan["flags"] if f not in doc["risk_flags"]]
        doc["risk_flags"] += chat_flags
        if scan["stop"]:
            ch["blocked"] = True
            doc["risk_level"] = "red"
            save(doc)
            return {"stop": True, "messages": scan["stop_messages"], "state": view(doc)}
        result["notices"] = [n["message"] for n in scan["notices"]]
        if scan["flags"]:
            doc["risk_level"] = "yellow"

    rec = {"day": day, "date": _today().isoformat(), "status": status, "barrier": barrier,
           "answer_key": q["key"] if q and answer else None, "answer": answer, "note": note}
    ch["checkins"] = [x for x in ch["checkins"] if x["day"] != day] + [rec]
    ch["checkins"].sort(key=lambda x: x["day"])
    if day == DAYS:
        doc["status"] = "in_progress"
    save(doc)

    msg = c["feedback"][status]
    extra = {"done": t.get("done_msg"), "partial": t.get("partial_msg")}.get(status)
    if status == "skipped":
        parts = [msg]
        if barrier:
            parts.append(c["barrier_tips"][barrier])
        parts.append(c["feedback"]["skipped_tail"])
        result["feedback"] = " ".join(parts)
    else:
        result["feedback"] = f"{msg}{extra}" if extra else msg
    if note:
        result["ai_reply"] = _llm_reply(doc, day, status, note)
    result["stop"] = False
    result["state"] = view(doc)
    return result


def set_interest(sid: str, payload: dict) -> dict:
    doc = _require(sid)
    doc["interest"] = {"coaching_interest": bool(payload.get("coaching_interest")), "notes": (payload.get("notes") or "").strip()[:200] or None}
    doc["user_confirmed"] = True
    if _checkin(doc, DAYS):
        doc["status"] = "completed"
    save(doc)
    return view(doc)


def advance(sid: str) -> dict:
    if not demo_enabled():
        raise ChallengeError("未开启演示模式", 403)
    doc = _require(sid)
    ch = doc["challenge"]
    if current_day(doc) < DAYS:
        ch["demo_offset_days"] = int(ch.get("demo_offset_days", 0)) + 1
        save(doc)
    return view(doc)


def config() -> dict:
    """入口页要显示的一次性提示文案。"""
    c = content()
    return {"title": c["title"], "subtitle": c["subtitle"], "days": DAYS, "notices": c["notices"]}
