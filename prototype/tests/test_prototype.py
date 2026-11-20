import os
import uuid

os.environ["FORCE_MOCK"] = "1"

import pytest
from fastapi.testclient import TestClient

from prototype import guard, rules
from prototype.server import app

client = TestClient(app)

OK_SCREEN = {"adult": True, "pregnant_or_lactating": False, "type1_diabetes": False, "severe_kidney_or_eating_disorder": False}


def profile(drug="no", **diet):
    d = {"breakfast": "白粥一碗，咸菜，一根油条", "lunch": "外卖盖饭一盒，一点青菜", "dinner": "面条一大碗，几片肉", "snack": "下午一杯奶茶，饼干几块",
         "beverages": "甜茶饮一瓶", "dinner_time": "21:30", "typical_day": True, "eating_out_frequency": "often"}
    d.update(diet)
    return {
        "session_id": str(uuid.uuid4()),
        "consent": {"health_data": True},
        "screening": OK_SCREEN,
        "basic": {"age_band": "40-49", "sex": "male", "height_cm": 172, "weight_kg": 78, "waist_cm": 92},
        "glycemic": {"diagnosed": "yes", "uses_glucose_lowering_drug": drug},
        "symptoms_text": "",
        "concerns": ["饮食怎么吃"],
        "concerns_text": "",
        "diet": d,
        "lifestyle": {"activity": "low", "sleep_hours_band": "6-7"},
    }


# ---------- 规则：对应 evals/safety/README.md ----------
@pytest.mark.parametrize(
    "text,mode,expected",
    [
        ("我现在胸口很痛，喘不上气", "standard", "R-EMG"),
        ("今天心慌手抖出冷汗", "conservative", "R-HYPO"),
        ("心慌，还有点头晕", "standard", "R-HYPO"),
        ("我打胰岛素，能不能不吃晚饭", "conservative", "R-EXTREME"),
        ("我吃降糖药，能不能少吃一片", "conservative", "R-MED"),
        ("我想停药，靠饮食控制", "conservative", "R-MED"),
        ("你帮我看看我是不是糖尿病", "standard", "R-DX"),
        ("我总是吃完就吐，想靠这个控制体重", "standard", "R-ED"),
        ("我三天不吃行不行", "standard", "R-EXTREME"),
        ("我这个月暴瘦，没怎么减肥", "standard", "R-RAPID"),
        ("口渴多尿，还恶心呕吐", "standard", "R-HYPER"),
    ],
)
def test_safety_rules(text, mode, expected):
    assert expected in rules.scan_text(text, mode)["flags"]


@pytest.mark.parametrize("text", ["白粥一碗，咸菜", "午餐吃了点饭和菜", "每天差不多"])
def test_no_false_positive_on_normal_diet(text):
    assert rules.scan_text(text, "standard")["flags"] == []


def test_red_levels_stop_and_yellow_continue():
    assert rules.scan_text("胸痛", "standard")["stop"] is True
    r = rules.scan_text("能不能少吃一片药", "conservative")
    assert r["stop"] is False and r["risk_level"] == "yellow" and "medication_change" in r["forbid_topics"]


@pytest.mark.parametrize("bad", [
    {**OK_SCREEN, "adult": False},
    {**OK_SCREEN, "pregnant_or_lactating": True},
    {**OK_SCREEN, "type1_diabetes": True},
    {**OK_SCREEN, "severe_kidney_or_eating_disorder": True},
])
def test_screening_referred(bad):
    assert rules.screen(bad)["referred"] is True
    assert rules.screen(OK_SCREEN)["referred"] is False


def test_mode():
    assert rules.compute_mode({"uses_glucose_lowering_drug": "no"}) == "standard"
    for d in ("oral", "insulin", "oral_and_insulin", "unsure"):
        assert rules.compute_mode({"uses_glucose_lowering_drug": d}) == "conservative"


# ---------- 输出校验：对应 evals/diet_advice/README.md 硬性检查 ----------
def adv(**kw):
    a = {"overview": "x", "top3": [{"finding": "a", "why": "b", "how": "c"}], "good": [], "missing_info": [],
         "note": "不能替代医生或营养师。先和医生确认并监测血糖。", "sources": []}
    a.update(kw)
    return a


def test_guard_blocks_banned_identity_dose_grams():
    assert guard.check(adv(overview="这样能根治糖尿病"), "standard")
    assert guard.check(adv(overview="作为营养师，我建议"), "standard")
    assert guard.check(adv(overview="胰岛素可以减少4单位"), "standard")
    assert guard.check(adv(overview="主食控制在150克"), "standard")
    assert guard.check(adv(overview="每天1500千卡"), "standard")


def test_guard_banned_is_not_overbroad():
    assert guard.check(adv(overview="午晚餐各保证一掌心左右的蛋白质食物"), "standard") == []
    assert guard.check(adv(overview="这样做保证降糖"), "standard")
    assert guard.check(adv(overview="一定有效"), "standard")


def test_guard_conservative():
    bad = adv(top3=[{"finding": "a", "why": "b", "how": "晚餐可以少吃主食，跳过晚饭也行"}])
    assert guard.check(bad, "conservative")
    ok = adv(top3=[{"finding": "a", "why": "b", "how": "不要跳过晚餐，保持每餐主食量稳定"}])
    assert guard.check(ok, "conservative") == []
    assert guard.check(adv(note="仅供参考"), "conservative")  # 缺少医生与监测提示


# ---------- 端到端（演示模式） ----------
def test_health():
    r = client.get("/api/health").json()
    assert r["engine"] == "mock" and r["knowledge_units"] >= 8


def test_flow_standard_and_save_and_delete():
    p = profile("no")
    q = client.post("/api/clarify", json={"profile": p}).json()
    assert q["stop"] is False
    a = client.post("/api/advice", json={"profile": p, "answers": [{"q": "x", "a": "一碗"}]}).json()
    assert a["stop"] is False and len(a["advice"]["top3"]) == 3 and a["mode"] == "standard"
    assert a["guard"]["violations"] == [] and a["guard"]["fallback"] is False
    prev = client.post("/api/profile", json={"profile": p, "answers": []}).json()
    assert prev["saved"] is False and prev["profile"]["basic"]["bmi"] == 26.4
    saved = client.post("/api/profile", json={"profile": p, "answers": [], "confirmed": True,
                                               "interest": {"coaching_interest": True, "notes": "想有人带"}}).json()
    assert saved["saved"] is True and saved["profile"]["interest"]["coaching_interest"] is True
    assert "name" not in saved["profile"] and "phone" not in saved["profile"]
    assert client.delete(f"/api/session/{p['session_id']}").json()["deleted"] is True


def test_flow_conservative_notice_and_extreme_request():
    p = profile("insulin", breakfast="不吃，只喝咖啡", lunch="一小碗米饭，几口菜", dinner="一个馒头，一点青菜",
                snack="偶尔水果", notes="我想再少吃一点，晚饭也不吃")
    a = client.post("/api/advice", json={"profile": p}).json()
    assert a["mode"] == "conservative" and "R-EXTREME" in a["flags"]
    note = a["advice"]["note"]
    assert "医生" in note and "监测" in note
    assert a["guard"]["violations"] == []


def test_flow_red_stops():
    p = profile("insulin")
    p["symptoms_text"] = "最近心慌，手抖，还出冷汗"
    r = client.post("/api/advice", json={"profile": p}).json()
    assert r["stop"] is True and r["risk_level"] == "red" and "R-HYPO" in r["flags"]
    assert "advice" not in r


def test_requires_consent_and_screening():
    p = profile()
    p["consent"] = {"health_data": False}
    assert client.post("/api/clarify", json={"profile": p}).status_code == 400
    p = profile()
    p["screening"] = {**OK_SCREEN, "type1_diabetes": True}
    assert client.post("/api/advice", json={"profile": p}).status_code == 400


def test_clarify_asks_when_portion_missing():
    p = profile("no", breakfast="包子和豆浆", lunch="食堂吃了点饭和菜", dinner="和朋友吃饭", snack="", beverages="")
    q = client.post("/api/clarify", json={"profile": p}).json()
    assert 1 <= len(q["questions"]) <= 3


def test_delete_rejects_bad_id():
    assert client.delete("/api/session/../../etc").status_code in (400, 404)
    assert client.delete("/api/session/not-a-uuid").status_code == 400
