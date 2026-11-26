import os
import uuid

os.environ["FORCE_MOCK"] = "1"

import pytest
from fastapi.testclient import TestClient

from prototype import challenge, guard, knowledge, server
from prototype.server import app

client = TestClient(app)
OK = {"adult": True, "pregnant_or_lactating": False, "type1_diabetes": False, "severe_kidney_or_eating_disorder": False}


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(challenge, "DATA", tmp_path)
    monkeypatch.setattr(server, "DATA", tmp_path)
    return tmp_path


def start(drug="no", **over):
    sid = str(uuid.uuid4())
    body = {"session_id": sid, "consent": True, "screening": OK, "uses_glucose_lowering_drug": drug, **over}
    r = client.post("/api/challenge/start", json=body)
    return sid, r


def ck(sid, **kw):
    return client.post(f"/api/challenge/{sid}/checkin", json=kw)


def adv(sid):
    return client.post(f"/api/challenge/{sid}/advance").json()


# ---------- 内容安全：任务库本身 ----------
def all_texts():
    c = challenge.content()
    for t in c["tasks"]:
        for k in ("title", "task", "why", "how", "note_conservative", "done_msg", "partial_msg"):
            if t.get(k):
                yield t[k]
        if t.get("question"):
            yield t["question"]["text"]
    yield from c["barrier_tips"].values()
    yield from c["feedback"].values()
    yield c["summary"]["invite"]


def test_option_labels_are_user_answers_not_advice():
    # 选项是用户描述自己情况的答案（如“我不吃早餐”），不是建议，不做建议类检查，只检查禁用词与药量
    for t in challenge.content()["tasks"]:
        for o in t["question"]["options"]:
            assert not any(w in o for w in guard.BANNED + guard.IDENTITY), o


@pytest.mark.parametrize("mode", ["standard", "conservative"])
def test_task_library_passes_guard(mode):
    for text in all_texts():
        assert guard.check_text(text, mode) == [], text


def test_task_library_has_no_reduction_advice():
    bad = ["少吃", "跳过", "不吃", "禁食", "节食", "戒掉", "断食"]
    for text in all_texts():
        for w in bad:
            if w in text:
                # 允许出现在否定提示里，如“不要跳过任何一餐”“不要因此少吃”
                assert any(n in text for n in ("不要", "不用", "不能")), text


def test_library_structure():
    c = challenge.content()
    assert [t["day"] for t in c["tasks"]] == list(range(1, 8))
    ids = {u["id"] for u in knowledge.load_units()}
    for t in c["tasks"]:
        assert set(t["kb_refs"]) <= ids, t["day"]
        assert t["question"]["options"] and t["question"]["key"]
    assert {b["id"] for b in c["barriers"]} == set(c["barrier_tips"])
    assert c["status"] == "pending_review"


# ---------- 开始 ----------
def test_start_ok_and_conservative_mode():
    sid, r = start("insulin")
    j = r.json()
    assert r.status_code == 200 and j["current_day"] == 1 and j["mode"] == "conservative"
    assert j["today"]["title"] and len(j["days"]) == 7
    sid2, r2 = start("no")
    assert r2.json()["mode"] == "standard"


def test_start_requires_consent_screening_and_drug():
    assert start(consent=False)[1].status_code == 400
    assert start(screening={**OK, "adult": None})[1].status_code == 400
    assert start(uses_glucose_lowering_drug="banana")[1].status_code == 400


@pytest.mark.parametrize("bad", [{"adult": False}, {"pregnant_or_lactating": True}, {"type1_diabetes": True},
                                 {"severe_kidney_or_eating_disorder": True}])
def test_start_excludes(bad):
    sid, r = start(screening={**OK, **bad})
    assert r.status_code == 403 and client.get(f"/api/challenge/{sid}").status_code == 404


def test_start_is_resumable():
    sid, _ = start("no")
    ck(sid, day=1, status="done")
    r = client.post("/api/challenge/start", json={"session_id": sid}).json()
    assert r["today_checkin"]["status"] == "done"


def test_bad_session_ids():
    assert client.get("/api/challenge/not-a-uuid").status_code == 400
    assert client.post("/api/challenge/not-a-uuid/checkin", json={}).status_code == 400


def test_medication_text_in_tasks_for_conservative_only():
    sid, _ = start("insulin")
    for _ in range(2):
        adv(sid)
    j = client.get(f"/api/challenge/{sid}").json()
    assert j["current_day"] == 3 and "医生" in j["today"]["note"]
    sid2, _ = start("no")
    for _ in range(2):
        adv(sid2)
    assert "note" not in client.get(f"/api/challenge/{sid2}").json()["today"]


# ---------- 打卡 ----------
def test_checkin_rules():
    sid, _ = start("no")
    assert ck(sid, day=2, status="done").status_code == 400  # 未解锁
    assert ck(sid, day=1, status="maybe").status_code == 400
    assert ck(sid, day=1, status="done", answer="火星").status_code == 400
    assert ck(sid, day=1, status="skipped", barrier="x").status_code == 400
    r = ck(sid, day=1, status="done", answer="晚餐").json()
    assert r["stop"] is False and "做到了" in r["feedback"] and r["state"]["today_checkin"]["answer"] == "晚餐"


def test_skipped_drops_answer_and_gives_barrier_tip():
    sid, _ = start("no")
    r = ck(sid, day=1, status="skipped", barrier="no_time", answer="晚餐").json()
    assert "没关系" in r["feedback"] and "不需要额外时间" in r["feedback"]
    rec = r["state"]["today_checkin"]
    assert rec["answer"] is None and rec["barrier"] == "no_time"


def test_checkin_overwrites_same_day():
    sid, _ = start("no")
    ck(sid, day=1, status="skipped", barrier="forgot")
    r = ck(sid, day=1, status="done").json()
    assert len(r["state"]["checkins"]) == 1 and r["state"]["today_checkin"]["status"] == "done"


def test_full_seven_days_summary_interest_delete(tmp_data):
    sid, _ = start("oral")
    statuses = ["done", "partial", "skipped", "done", "done", "skipped", "done"]
    for i, s in enumerate(statuses, start=1):
        kw = {"day": i, "status": s}
        if s == "skipped":
            kw["barrier"] = "eating_out"
        elif i == 7:
            kw["answer"] = "先吃蔬菜"
        r = ck(sid, **kw)
        assert r.status_code == 200
        if i < 7:
            adv(sid)
    st = client.get(f"/api/challenge/{sid}").json()
    sm = st["summary"]
    assert sm["done"] == 4 and sm["partial"] == 1 and sm["skipped"] == 2 and sm["top_barrier"] == "在外面吃，不方便"
    assert any("先吃蔬菜" in line for line in sm["lines"])
    r = client.post(f"/api/challenge/{sid}/interest", json={"coaching_interest": True, "notes": "想有人带"}).json()
    assert r["interest"]["coaching_interest"] is True and r["confirmed"] is True
    import json
    doc = json.loads((tmp_data / f"{sid}.json").read_text(encoding="utf-8"))
    assert doc["status"] == "completed" and len(doc["challenge"]["checkins"]) == 7
    assert "name" not in doc and "phone" not in doc
    assert client.delete(f"/api/session/{sid}").json()["deleted"] is True
    assert client.get(f"/api/challenge/{sid}").status_code == 404


def test_advance_stops_at_day_seven():
    sid, _ = start("no")
    for _ in range(10):
        j = adv(sid)
    assert j["current_day"] == 7


# ---------- 备注与风险 ----------
def test_red_note_blocks_challenge():
    sid, _ = start("insulin")
    r = ck(sid, day=1, status="partial", note="今天心慌手抖出冷汗").json()
    assert r["stop"] is True and r["messages"]
    st = client.get(f"/api/challenge/{sid}").json()
    assert st["blocked"] is True and st["stop_messages"] and "today" not in st
    assert ck(sid, day=1, status="done").status_code == 409


def test_yellow_note_gives_notice_not_block():
    sid, _ = start("insulin")
    r = ck(sid, day=1, status="done", note="我想自己少吃一片药").json()
    assert r["stop"] is False and r["notices"]


def test_ai_reply_only_with_note_and_guarded(monkeypatch):
    sid, _ = start("no")
    assert "ai_reply" not in ck(sid, day=1, status="done").json()
    r = ck(sid, day=1, status="done", note="今天换了无糖茶，感觉还行").json()
    assert r["ai_reply"]
    from prototype import llm
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"reply": "这样可以根治糖尿病"})
    r = ck(sid, day=1, status="done", note="又做了一次").json()
    assert r["ai_reply"] is None
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ck(sid, day=1, status="done", note="再来").json()["ai_reply"] is None


def test_home_page_served():
    assert client.get("/").status_code == 200 and client.get("/chat").status_code == 200
