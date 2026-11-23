"""对话式入口（雏形）。

原则：
1. 先回答用户自己的问题，不先填表。一般性问题不需要建档。
2. 规则引擎在每条消息上先于模型执行。
3. 要给“针对个人”的建议之前，才由代码（不是模型）依次确认：同意 → 年龄 → 孕期 → 1 型 → 肾病或进食障碍 → 用药。
4. 用药情况未确认前，一律按保守处理。
5. 档案在对话中逐步形成，用户确认后才保存。
"""
from __future__ import annotations

import datetime as dt
import json
import re

from . import guard, knowledge, llm, prompts, rules

DRUG_VALUES = {"no", "oral", "insulin", "oral_and_insulin", "unsure"}
DRUG_WORDS = ["胰岛素", "降糖药", "二甲双胍", "格列", "诺和", "来得时", "打针", "控糖药"]
DIET_WORDS = ["早餐", "早饭", "午餐", "午饭", "晚餐", "晚饭", "吃了", "喝了", "早上吃", "中午吃", "晚上吃"]
AGE_BANDS = {"18-29", "30-39", "40-49", "50-59", "60-69", "70+"}

GATES = [
    ("consent", "要给你针对个人的建议，需要用到你的饮食和健康信息。这些只用来生成建议，不要求姓名和手机号，你可以随时删除。是否同意？",
     [("同意", True), ("暂不", False)]),
    ("adult", "你是否已满 18 岁？", [("是", True), ("否", False)]),
    ("pregnant_or_lactating", "你目前是否处于孕期或哺乳期？", [("是", True), ("否", False)]),
    ("type1_diabetes", "你是否是 1 型糖尿病？", [("是", True), ("否", False)]),
    ("severe_kidney_or_eating_disorder", "你是否有严重肾病，或有进食障碍（如催吐、暴食）？", [("是", True), ("否", False)]),
    ("uses_glucose_lowering_drug", "你目前是否在使用降糖药物？",
     [("没有使用", "no"), ("口服药", "oral"), ("胰岛素", "insulin"), ("口服药和胰岛素", "oral_and_insulin"), ("不确定", "unsure")]),
]
SCREEN_KEYS = ["adult", "pregnant_or_lactating", "type1_diabetes", "severe_kidney_or_eating_disorder"]
GATE_KEYS = {g[0] for g in GATES}


# ---------------------------------------------------------------- 状态
def normalize(state: dict) -> dict:
    s = dict(state or {})
    s.setdefault("session_id", "")
    s.setdefault("consent", None)
    s["screening"] = dict(s.get("screening") or {})
    s["glycemic"] = dict(s.get("glycemic") or {})
    s["basic"] = dict(s.get("basic") or {})
    s.setdefault("concerns", [])
    s.setdefault("diet_text", None)
    s.setdefault("stage", None)  # None / awaiting_diet / awaiting_clarify
    s.setdefault("clarified", False)
    s.setdefault("answers", [])
    s.setdefault("mentions_drug", False)
    s.setdefault("flags", [])
    s.setdefault("referred", False)
    s.setdefault("blocked", False)
    s.setdefault("advice_done", False)
    return s


def mode_of(s: dict) -> str:
    drug = s["glycemic"].get("uses_glucose_lowering_drug")
    if drug is None:
        return "conservative"
    if drug == "no" and not s["mentions_drug"]:
        return "standard"
    return "conservative"


def _gate_value(s: dict, key: str):
    if key == "consent":
        return s["consent"]
    if key == "uses_glucose_lowering_drug":
        return s["glycemic"].get(key)
    return s["screening"].get(key)


def next_gate(s: dict):
    for key, q, opts in GATES:
        if _gate_value(s, key) is None:
            return {"type": "gate", "key": key, "text": q, "options": [{"label": l, "value": v} for l, v in opts]}
    return None


def excluded(s: dict) -> bool:
    sc = s["screening"]
    return sc.get("adult") is False or any(sc.get(k) is True for k in SCREEN_KEYS[1:])


def _risk_level(flags: list[str]) -> str:
    defs = rules.rule_defs()
    if any(defs[f]["action"].get("stop_nutrition_advice") for f in flags):
        return "red"
    return "yellow" if flags else "green"


def _add_flags(s: dict, flags: list[str]) -> None:
    for f in flags:
        if f not in s["flags"]:
            s["flags"].append(f)
    if _risk_level(s["flags"]) == "red":
        s["blocked"] = True  # 本次会话出现过红级信号，不再提供个性化饮食建议


def _apply_slots(s: dict, upd: dict) -> None:
    """模型提取的信息只接受白名单字段并校验取值。用药情况只能由用户点选确认，不接受模型推断。"""
    if not isinstance(upd, dict):
        return
    cs = upd.get("concerns")
    if isinstance(cs, list):
        for c in cs[:3]:
            if isinstance(c, str) and 0 < len(c.strip()) <= 12 and c.strip() not in s["concerns"] and len(s["concerns"]) < 6:
                s["concerns"].append(c.strip())
    if upd.get("age_band") in AGE_BANDS:
        s["basic"]["age_band"] = upd["age_band"]
    if upd.get("sex") in ("female", "male"):
        s["basic"]["sex"] = upd["sex"]


def _looks_like_diet(msg: str) -> bool:
    return any(w in msg for w in DIET_WORDS) or len(msg) >= 25


# ---------------------------------------------------------------- 主入口
def handle(payload: dict) -> dict:
    s = normalize(payload.get("state"))
    history = [h for h in (payload.get("history") or [])[-8:] if h.get("role") in ("user", "assistant") and isinstance(h.get("content"), str)]
    msg = (payload.get("message") or "").strip()[:1000]
    action = payload.get("action") or {}
    events: list[dict] = []

    # --- 动作：点选确认 / 想要个性化建议
    personal = False
    if action.get("type") == "gate_answer":
        key, val = action.get("key"), action.get("value")
        if key not in GATE_KEYS:
            return {"state": s, "events": [{"type": "text", "text": "无效的选择。"}]}
        if key == "consent":
            s["consent"] = bool(val)
            if not s["consent"]:
                s["stage"] = None
                return {"state": s, "events": [{"type": "text", "text": "好的，那我们继续聊一般性的问题。需要的时候，随时可以再让我帮你看一天的饮食。"}]}
        elif key == "uses_glucose_lowering_drug":
            if val not in DRUG_VALUES:
                return {"state": s, "events": [{"type": "text", "text": "无效的选择。"}]}
            s["glycemic"][key] = val
        else:
            s["screening"][key] = bool(val)
        personal = True
    elif action.get("type") == "want_personal":
        personal = True

    if personal:
        return _personal(s, events, history)

    if not msg:
        return {"state": s, "events": events}

    # --- 用户消息：规则前置
    if any(w in msg for w in DRUG_WORDS):
        s["mentions_drug"] = True
    mode = mode_of(s)
    scan = rules.scan_text(msg, mode)
    _add_flags(s, scan["flags"])
    if scan["stop"]:
        s["stage"] = None
        events.append({"type": "stop", "messages": scan["stop_messages"]})
        return {"state": s, "events": events}
    for n in scan["notices"]:
        events.append({"type": "notice", "text": n["message"]})

    # --- 个性化流程中的输入
    if s["stage"] == "awaiting_diet" and _looks_like_diet(msg):
        s["diet_text"] = msg
        return _personal(s, events, history)
    if s["stage"] == "awaiting_clarify":
        s["answers"] = [{"q": "补充信息", "a": msg}]
        s["stage"] = None
        return _personal(s, events, history, forbid=scan["forbid_topics"])

    # --- 一般性问答
    return _general(s, events, history, msg, mode, scan["forbid_topics"])


# ---------------------------------------------------------------- 一般问答
def _known(s: dict) -> dict:
    k = {}
    if s["concerns"]:
        k["关注点"] = s["concerns"]
    k.update({"年龄段": s["basic"].get("age_band"), "性别": s["basic"].get("sex")})
    return {a: b for a, b in k.items() if b}


def _general(s, events, history, msg, mode, forbid):
    units = knowledge.retrieve(" ".join([h["content"] for h in history if h["role"] == "user"][-2:] + [msg]))
    system = prompts.chat_system(mode, units)
    feedback, out, violations = None, None, []
    for _ in range(2):
        try:
            cand = llm.chat_json(system, prompts.chat_user(history, msg, _known(s), forbid, feedback), "chat",
                                 {"units": [{"id": u["id"], "topic": u["topic"], "core": u["core"]} for u in units], "mode": mode})
        except RuntimeError:
            break
        reply = str(cand.get("reply") or "").strip()
        follow = str(cand.get("follow_up") or "").strip()
        violations = guard.check_text(reply + "\n" + follow, mode)
        if reply and not violations:
            out = {"reply": reply, "follow_up": follow, "cand": cand}
            break
        feedback = violations or ["回答为空"]
    if out is None:
        events.append({"type": "answer", "text": guard.CHAT_FALLBACK, "follow_up": "", "quick_replies": [], "sources": [],
                       "guard": {"fallback": True, "violations": violations}})
        return {"state": s, "events": events}
    cand = out["cand"]
    _apply_slots(s, cand.get("slot_updates"))
    qr = [q for q in (cand.get("quick_replies") or []) if isinstance(q, str) and 0 < len(q) <= 20][:3]
    valid = {u["id"]: u for u in units}
    srcs = [{"id": i, "topic": valid[i]["topic"], "status": valid[i]["status"]} for i in cand.get("sources", []) if i in valid]
    events.append({"type": "answer", "text": out["reply"], "follow_up": out["follow_up"], "quick_replies": qr, "sources": srcs,
                   "guard": {"fallback": False, "violations": []}})
    return {"state": s, "events": events}


# ---------------------------------------------------------------- 个性化流程
def _personal(s, events, history, forbid=None):
    if excluded(s):
        s["referred"] = True
        s["stage"] = None
        _add_flags(s, ["R-EXCL"])
        events.append({"type": "referred", "text": rules.message("MSG-EXCL")})
        return {"state": s, "events": events}

    if s["blocked"]:
        defs = rules.rule_defs()
        stop_ids = [f for f in s["flags"] if defs[f]["action"].get("stop_nutrition_advice")]
        s["stage"] = None
        events.append({"type": "stop", "messages": [rules.message(defs[f]["action"]["message_id"]) for f in stop_ids[:1]]})
        return {"state": s, "events": events}

    gate = next_gate(s)
    if gate:
        events.append(gate)
        return {"state": s, "events": events}

    if not s["diet_text"]:
        s["stage"] = "awaiting_diet"
        events.append({"type": "ask_diet", "text": "好的。把你一天吃的写给我就行：早餐、午餐、晚餐、加餐、饮料都可以，尽量带上大概的份量，比如“一碗”“两个”“一拳头”。"})
        return {"state": s, "events": events}

    mode = mode_of(s)
    scan = rules.scan_text(s["diet_text"] + "\n" + " ".join(a["a"] for a in s["answers"]), mode)
    _add_flags(s, scan["flags"])
    if scan["stop"]:
        s["stage"] = None
        events.append({"type": "stop", "messages": scan["stop_messages"]})
        return {"state": s, "events": events}

    view = {"mode": mode, "basic": s["basic"], "glycemic": {"uses_glucose_lowering_drug": s["glycemic"].get("uses_glucose_lowering_drug"),
            "diagnosed": s["glycemic"].get("diagnosed")}, "concerns": s["concerns"], "diet_log": {"free_text": s["diet_text"]}}
    units = knowledge.retrieve(s["diet_text"] + " " + " ".join(a["a"] for a in s["answers"]))

    if not s["clarified"]:
        s["clarified"] = True
        out = llm.chat_json(prompts.system_prompt(mode, "clarify", units), prompts.user_prompt(view, [], scan["forbid_topics"]),
                            "clarify", {"diet_log": view["diet_log"], "mode": mode})
        qs = [q["text"] for q in out.get("questions", []) if q.get("text")][:3]
        if qs:
            s["stage"] = "awaiting_clarify"
            events.append({"type": "clarify", "questions": qs})
            return {"state": s, "events": events}

    sys_prompt = prompts.system_prompt(mode, "advice", units)
    ctx = {"diet_log": view["diet_log"], "mode": mode, "answers": s["answers"]}
    advice, violations, fallback, feedback = None, [], False, None
    for _ in range(2):
        try:
            cand = llm.chat_json(sys_prompt, prompts.user_prompt(view, s["answers"], scan["forbid_topics"], feedback), "advice", ctx)
        except RuntimeError as e:
            violations = [str(e)]
            break
        cand["top3"] = (cand.get("top3") or [])[:3]
        cand = guard.ensure_note(cand, mode)
        violations = guard.check(cand, mode)
        if not violations:
            advice = cand
            break
        feedback = violations
    if advice is None:
        advice, fallback = guard.safe_fallback(mode), True
    valid = {u["id"]: u for u in units}
    advice["sources"] = [i for i in advice.get("sources", []) if i in valid]
    s["stage"] = None
    s["advice_done"] = True
    events.append({
        "type": "advice", "advice": advice, "mode": mode, "notices": scan["notices"],
        "sources": [{"id": i, "topic": valid[i]["topic"], "status": valid[i]["status"]} for i in advice["sources"]],
        "guard": {"violations": violations, "fallback": fallback},
    })
    return {"state": s, "events": events}


# ---------------------------------------------------------------- 档案
def build_doc(s: dict, confirmed: bool, interest: dict) -> dict:
    if s["consent"] is not True or any(s["screening"].get(k) is None for k in SCREEN_KEYS) or excluded(s):
        raise ValueError("尚未完成同意和安全确认，或不符合服务条件")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    basic = {k: v for k, v in s["basic"].items() if v is not None}
    drug = s["glycemic"].get("uses_glucose_lowering_drug") or "unsure"
    flags = list(s["flags"])
    return {
        "session_id": s["session_id"],
        "source": "prototype-chat",
        "consent": {"health_data": True, "interest_contact": False, "version": "demo-0.1", "timestamp": now},
        "screening": {k: bool(s["screening"][k]) for k in SCREEN_KEYS},
        "mode": mode_of(s),
        "basic": basic,
        "glycemic": {"diagnosed": s["glycemic"].get("diagnosed", "unsure"), "recent_indicators": [], "uses_glucose_lowering_drug": drug},
        "diet_log": {"typical_day": True, "meals": [], "raw_text": s["diet_text"], "beverages": [], "eating_out_frequency": "unknown"},
        "lifestyle": {},
        "concerns": list(s["concerns"]),
        "risk_level": _risk_level(flags),
        "risk_flags": flags,
        "advice_summary_id": None,
        "interest": {"coaching_interest": bool(interest.get("coaching_interest")), "notes": (interest.get("notes") or None)},
        "user_confirmed": confirmed,
        "status": "completed" if confirmed else "in_progress",
        "created_at": now,
        "updated_at": now,
    }
