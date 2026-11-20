"""SeekNutri 第一期用户端雏形（FastAPI）。

运行：uvicorn prototype.server:app --reload --port 8000
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
import uuid

import jsonschema
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import guard, knowledge, llm, prompts, rules

BASE = pathlib.Path(__file__).resolve().parent
DATA = BASE / "data"
SCHEMA = json.loads((BASE.parent / "schemas" / "user_profile.schema.json").read_text(encoding="utf-8"))
PRIVACY_VERSION = "demo-0.1"

app = FastAPI(title="SeekNutri Phase 1 Prototype")


# ---------------------------------------------------------------- 工具函数
def _split(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"[，,；;、\n]+", s or "") if x.strip()]


def _bmi(basic: dict) -> float | None:
    try:
        h, w = float(basic.get("height_cm")), float(basic.get("weight_kg"))
        return round(w / ((h / 100) ** 2), 1) if h > 0 and w > 0 else None
    except (TypeError, ValueError):
        return None


def _diet_log(p: dict) -> dict:
    d = p.get("diet", {})
    meals = []
    for key in ("breakfast", "lunch", "dinner", "snack"):
        items = [{"food": f} for f in _split(d.get(key, ""))]
        if items:
            m = {"meal": key, "items": items}
            if key == "dinner" and d.get("dinner_time"):
                m["time"] = d["dinner_time"]
            meals.append(m)
    return {
        "typical_day": bool(d.get("typical_day", True)),
        "meals": meals,
        "beverages": _split(d.get("beverages", "")),
        "eating_out_frequency": d.get("eating_out_frequency", "unknown"),
    }


def _all_text(p: dict, answers: list[dict]) -> str:
    d = p.get("diet", {})
    parts = [
        p.get("symptoms_text", ""),
        p.get("concerns_text", ""),
        " ".join(p.get("concerns", [])),
        *(d.get(k, "") for k in ("breakfast", "lunch", "dinner", "snack", "beverages", "notes")),
        *(a.get("a", "") for a in answers),
    ]
    return "\n".join(x for x in parts if x)


def _profile_view(p: dict, mode: str) -> dict:
    """给模型看的内容：不含身份信息。"""
    basic = dict(p.get("basic", {}))
    basic["bmi"] = _bmi(basic)
    return {
        "mode": mode,
        "basic": basic,
        "glycemic": {
            "diagnosed": p.get("glycemic", {}).get("diagnosed"),
            "uses_glucose_lowering_drug": p.get("glycemic", {}).get("uses_glucose_lowering_drug"),
        },
        "concerns": p.get("concerns", []) + ([p["concerns_text"]] if p.get("concerns_text") else []),
        "lifestyle": p.get("lifestyle", {}),
        "diet_log": _diet_log(p),
    }


def build_doc(p: dict, answers: list[dict], scan: dict, mode: str, confirmed: bool, interest: dict) -> dict:
    g = p.get("glycemic", {})
    indicators = []
    ind = g.get("indicator") or {}
    if ind.get("value") not in (None, "") and ind.get("measured_on"):
        indicators.append(
            {"type": ind.get("type", "other"), "value": float(ind["value"]), "unit": ind.get("unit", ""), "measured_on": ind["measured_on"]}
        )
    basic = {k: p.get("basic", {}).get(k) for k in ("age_band", "sex")}
    for k in ("height_cm", "weight_kg", "waist_cm"):
        v = p.get("basic", {}).get(k)
        basic[k] = float(v) if v not in (None, "") else None
    basic = {k: v for k, v in basic.items() if v is not None}
    basic["bmi"] = _bmi(p.get("basic", {}))
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    doc = {
        "session_id": p["session_id"],
        "source": p.get("source", "prototype"),
        "consent": {"health_data": True, "interest_contact": False, "version": PRIVACY_VERSION, "timestamp": now},
        "screening": p["screening"],
        "mode": mode,
        "basic": basic,
        "glycemic": {
            "diagnosed": g.get("diagnosed", "unsure"),
            "recent_indicators": indicators,
            "uses_glucose_lowering_drug": g.get("uses_glucose_lowering_drug", "unsure"),
        },
        "diet_log": _diet_log(p),
        "lifestyle": p.get("lifestyle", {}),
        "concerns": p.get("concerns", []) + _split(p.get("concerns_text", "")),
        "risk_level": scan["risk_level"],
        "risk_flags": scan["flags"],
        "advice_summary_id": None,
        "interest": {"coaching_interest": bool(interest.get("coaching_interest")), "notes": interest.get("notes") or None},
        "user_confirmed": confirmed,
        "status": "completed" if confirmed else "in_progress",
        "created_at": now,
        "updated_at": now,
    }
    return doc


def _validate(doc: dict) -> None:
    try:
        jsonschema.validate(doc, SCHEMA)
    except jsonschema.ValidationError as e:
        raise HTTPException(500, f"档案不符合数据结构：{e.message}")


def _require(p: dict) -> tuple[str, dict]:
    if not p.get("consent", {}).get("health_data"):
        raise HTTPException(400, "需要先同意健康信息说明")
    if not p.get("session_id"):
        raise HTTPException(400, "缺少会话标识")
    sc = rules.screen(p.get("screening", {}))
    if sc["referred"]:
        raise HTTPException(400, "不符合服务条件")
    return rules.compute_mode(p.get("glycemic", {})), sc


# ---------------------------------------------------------------- 接口
@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"engine": llm.engine_name(), "knowledge_units": len(knowledge.load_units())}


@app.post("/api/screen")
def api_screen(payload: dict = Body(...)):
    return rules.screen(payload.get("screening", {}))


@app.post("/api/clarify")
def api_clarify(payload: dict = Body(...)):
    p = payload["profile"]
    mode, _ = _require(p)
    scan = rules.scan_text(_all_text(p, []), mode)
    if scan["stop"]:
        return {"stop": True, "risk_level": "red", "messages": scan["stop_messages"], "flags": scan["flags"]}
    view = _profile_view(p, mode)
    units = knowledge.retrieve(json.dumps(view["diet_log"], ensure_ascii=False))
    out = llm.chat_json(
        prompts.system_prompt(mode, "clarify", units),
        prompts.user_prompt(view, [], scan["forbid_topics"]),
        "clarify",
        {"diet_log": view["diet_log"], "mode": mode},
    )
    qs = [q for q in out.get("questions", []) if q.get("text")][:3]
    return {"stop": False, "questions": qs, "notices": scan["notices"], "flags": scan["flags"], "engine": llm.engine_name()}


@app.post("/api/advice")
def api_advice(payload: dict = Body(...)):
    p = payload["profile"]
    answers = payload.get("answers", [])
    mode, _ = _require(p)
    scan = rules.scan_text(_all_text(p, answers), mode)
    if scan["stop"]:
        return {"stop": True, "risk_level": "red", "messages": scan["stop_messages"], "flags": scan["flags"]}

    view = _profile_view(p, mode)
    units = knowledge.retrieve(json.dumps(view["diet_log"], ensure_ascii=False) + json.dumps(answers, ensure_ascii=False))
    sys_prompt = prompts.system_prompt(mode, "advice", units)
    ctx = {"diet_log": view["diet_log"], "mode": mode, "answers": answers}

    advice, violations, used_fallback, feedback = None, [], False, None
    for _ in range(2):
        try:
            cand = llm.chat_json(sys_prompt, prompts.user_prompt(view, answers, scan["forbid_topics"], feedback), "advice", ctx)
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
        advice, used_fallback = guard.safe_fallback(mode), True

    valid_ids = {u["id"] for u in units}
    advice["sources"] = [s for s in advice.get("sources", []) if s in valid_ids]
    return {
        "stop": False,
        "risk_level": scan["risk_level"],
        "flags": scan["flags"],
        "notices": scan["notices"],
        "mode": mode,
        "advice": advice,
        "sources": [{"id": u["id"], "topic": u["topic"], "status": u["status"]} for u in units if u["id"] in advice["sources"]],
        "engine": llm.engine_name(),
        "guard": {"violations": violations, "fallback": used_fallback},
    }


@app.post("/api/profile")
def api_profile(payload: dict = Body(...)):
    """预览或保存档案。confirmed=true 时落盘。"""
    p = payload["profile"]
    answers = payload.get("answers", [])
    mode, _ = _require(p)
    scan = rules.scan_text(_all_text(p, answers), mode)
    confirmed = bool(payload.get("confirmed"))
    doc = build_doc(p, answers, scan, mode, confirmed, payload.get("interest", {}))
    _validate(doc)
    if confirmed:
        DATA.mkdir(exist_ok=True)
        (DATA / f"{doc['session_id']}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"saved": confirmed, "profile": doc}


@app.delete("/api/session/{session_id}")
def api_delete(session_id: str):
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(400, "无效的会话标识")
    f = DATA / f"{session_id}.json"
    existed = f.exists()
    if existed:
        f.unlink()
    return {"deleted": existed}
