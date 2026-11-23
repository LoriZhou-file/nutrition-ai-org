import os
import uuid

os.environ["FORCE_MOCK"] = "1"

import pytest
from fastapi.testclient import TestClient

from prototype import chat
from prototype.server import app

client = TestClient(app)


class Conv:
    """模拟前端：持有 state，逐次调用 /api/chat。"""

    def __init__(self):
        self.state = {"session_id": str(uuid.uuid4())}
        self.history = []

    def send(self, **kw):
        r = client.post("/api/chat", json={"state": self.state, "history": self.history, **kw}).json()
        self.state = r["state"]
        return r["events"]

    def gate(self, key, value):
        return self.send(action={"type": "gate_answer", "key": key, "value": value})

    def pass_gates(self, drug="no"):
        self.send(action={"type": "want_personal"})
        for k, v in [("consent", True), ("adult", True), ("pregnant_or_lactating", False), ("type1_diabetes", False),
                     ("severe_kidney_or_eating_disorder", False), ("uses_glucose_lowering_drug", drug)]:
            ev = self.gate(k, v)
        return ev


def types(ev):
    return [e["type"] for e in ev]


def test_general_question_needs_no_gates():
    c = Conv()
    ev = c.send(message="血糖高还能吃水果吗")
    assert types(ev) == ["answer"]
    assert ev[0]["text"] and c.state["consent"] is None and c.state["stage"] is None


def test_personal_flow_asks_gates_in_order_then_diet():
    c = Conv()
    ev = c.send(action={"type": "want_personal"})
    assert ev[0]["type"] == "gate" and ev[0]["key"] == "consent"
    keys = []
    for k, v in [("consent", True), ("adult", True), ("pregnant_or_lactating", False), ("type1_diabetes", False),
                 ("severe_kidney_or_eating_disorder", False)]:
        ev = c.gate(k, v)
        keys.append(ev[0]["key"])
    assert keys == ["adult", "pregnant_or_lactating", "type1_diabetes", "severe_kidney_or_eating_disorder", "uses_glucose_lowering_drug"]
    ev = c.gate("uses_glucose_lowering_drug", "no")
    assert types(ev) == ["ask_diet"] and c.state["stage"] == "awaiting_diet"


def test_full_flow_to_advice_and_profile_save_delete():
    c = Conv()
    c.pass_gates("insulin")
    ev = c.send(message="早餐包子和豆浆，午餐食堂吃了点饭和菜，晚餐和朋友吃饭")
    assert types(ev) == ["clarify"] and c.state["stage"] == "awaiting_clarify"
    ev = c.send(message="一碗饭，半糖")
    adv = [e for e in ev if e["type"] == "advice"][0]
    assert adv["mode"] == "conservative" and len(adv["advice"]["top3"]) == 3
    assert "医生" in adv["advice"]["note"] and "监测" in adv["advice"]["note"]
    assert adv["guard"]["violations"] == []
    prev = client.post("/api/chat/profile", json={"state": c.state}).json()
    assert prev["saved"] is False and prev["profile"]["mode"] == "conservative"
    assert prev["profile"]["diet_log"]["raw_text"].startswith("早餐")
    saved = client.post("/api/chat/profile", json={"state": c.state, "confirmed": True, "interest": {"coaching_interest": True}}).json()
    assert saved["saved"] is True and "phone" not in saved["profile"]
    assert client.delete(f"/api/session/{c.state['session_id']}").json()["deleted"] is True


@pytest.mark.parametrize("key,val", [("adult", False), ("pregnant_or_lactating", True), ("type1_diabetes", True),
                                     ("severe_kidney_or_eating_disorder", True)])
def test_exclusion_refers_and_blocks(key, val):
    c = Conv()
    c.send(action={"type": "want_personal"})
    c.gate("consent", True)
    seq = ["adult", "pregnant_or_lactating", "type1_diabetes", "severe_kidney_or_eating_disorder"]
    for k in seq:
        ev = c.gate(k, val if k == key else (k == "adult"))
        if k == key:
            assert types(ev) == ["referred"] and c.state["referred"] is True
            break
    # 转介后不能再给个性化建议，也不能生成档案
    r = client.post("/api/chat/profile", json={"state": c.state})
    assert r.status_code == 400


def test_decline_consent_keeps_general_chat():
    c = Conv()
    c.send(action={"type": "want_personal"})
    ev = c.gate("consent", False)
    assert types(ev) == ["text"] and c.state["consent"] is False
    assert types(c.send(message="主食怎么吃")) == ["answer"]


def test_rules_run_on_every_message_before_model():
    c = Conv()
    ev = c.send(message="我现在胸口很痛，喘不上气")
    assert types(ev) == ["stop"] and "R-EMG" in c.state["flags"]
    ev = c.send(message="我打胰岛素，能不能不吃晚饭")
    assert ev[0]["type"] == "notice" and ev[-1]["type"] == "answer"
    assert c.state["mentions_drug"] is True and "R-EXTREME" in c.state["flags"]


def test_stop_during_diet_input():
    c = Conv()
    c.pass_gates("insulin")
    ev = c.send(message="早餐一碗粥，最近心慌手抖出冷汗")
    assert types(ev) == ["stop"] and c.state["stage"] is None and c.state["advice_done"] is False


def test_unknown_medication_is_conservative_and_not_downgraded():
    s = chat.normalize({})
    assert chat.mode_of(s) == "conservative"
    s["glycemic"]["uses_glucose_lowering_drug"] = "no"
    assert chat.mode_of(s) == "standard"
    s["mentions_drug"] = True
    assert chat.mode_of(s) == "conservative"


def test_model_cannot_set_medication_or_bad_slots():
    s = chat.normalize({})
    chat._apply_slots(s, {"uses_glucose_lowering_drug": "no", "age_band": "30-39", "sex": "robot", "concerns": ["晚上想吃东西", "x" * 50]})
    assert s["glycemic"].get("uses_glucose_lowering_drug") is None
    assert s["basic"] == {"age_band": "30-39"} and s["concerns"] == ["晚上想吃东西"]


def test_question_while_awaiting_diet_is_answered_not_treated_as_diet():
    c = Conv()
    c.pass_gates("no")
    ev = c.send(message="水果呢")
    assert types(ev) == ["answer"] and c.state["diet_text"] is None and c.state["stage"] == "awaiting_diet"


def test_gate_input_validation():
    c = Conv()
    ev = c.send(action={"type": "gate_answer", "key": "evil", "value": True})
    assert ev[0]["type"] == "text"
    ev = c.send(action={"type": "gate_answer", "key": "uses_glucose_lowering_drug", "value": "banana"})
    assert ev[0]["type"] == "text" and c.state["glycemic"].get("uses_glucose_lowering_drug") is None


def test_chat_guard_blocks_bad_reply(monkeypatch):
    from prototype import llm

    calls = {"n": 0}

    def bad(system, user, kind, ctx, temperature=0.3):
        calls["n"] += 1
        return {"reply": "吃这个可以根治糖尿病，每天控制在100克", "follow_up": "", "quick_replies": [], "slot_updates": {}, "sources": []}

    monkeypatch.setattr(llm, "chat_json", bad)
    c = Conv()
    ev = c.send(message="怎么吃能好")
    assert calls["n"] == 2 and ev[0]["guard"]["fallback"] is True and "根治" not in ev[0]["text"]


def test_pages_served():
    assert "SeekNutri" in client.get("/").text and "chat" not in client.get("/").text[:0]
    assert client.get("/form").status_code == 200


def test_red_signal_blocks_personal_advice_for_session():
    c = Conv()
    ev = c.send(message="我打胰岛素，今天心慌手抖出冷汗")
    assert types(ev) == ["stop"] and c.state["blocked"] is True
    ev = c.send(action={"type": "want_personal"})
    assert types(ev) == ["stop"]
    # 一般性问答仍可进行
    assert types(c.send(message="主食怎么吃")) == ["answer"]
    # 仍不能生成档案以外的个性化建议；档案因未完成确认而不可用
    assert c.state["advice_done"] is False
