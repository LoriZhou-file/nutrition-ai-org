"""演示模式：没有模型密钥时，用简单规则生成示例输出，只用于看流程和界面。"""
from __future__ import annotations

import re

QUANT = "碗个份片块杯瓶拳勺两半把盘根只条袋颗"


def _all_meal_text(ctx: dict) -> str:
    d = ctx.get("diet_log", {})
    parts = [d.get("free_text", "")] if d.get("free_text") else []
    for m in d.get("meals", []):
        parts += [it.get("food", "") for it in m.get("items", [])]
    parts += d.get("beverages", [])
    return "；".join(parts)


def respond(kind: str, ctx: dict) -> dict:
    if kind == "chat":
        return _chat(ctx)
    if kind == "feedback":
        return {"reply": "（演示模式）收到你写的这一句，谢谢你认真记录，继续保持。"}
    return _clarify(ctx) if kind == "clarify" else _advice(ctx)


def _chat(ctx: dict) -> dict:
    units = ctx.get("units", [])
    if units:
        u = units[0]
        reply = f"（演示模式）关于「{u['topic']}」：{u['core']}"[:170]
        sources = [u["id"]]
    else:
        reply = "（演示模式）这个问题我需要查更多资料才能回答得稳妥。"
        sources = []
    return {
        "reply": reply,
        "follow_up": "你平时最困扰的是哪一餐？",
        "quick_replies": ["早餐", "午餐", "晚餐"],
        "slot_updates": {},
        "sources": sources,
    }


def _clarify(ctx: dict) -> dict:
    free = ctx.get("diet_log", {}).get("free_text")
    if free:
        qs = []
        if not re.search(f"[{QUANT}]", free):
            qs.append({"id": "q1", "text": "每餐大概吃了多少？可以用拳头、碗或几个来表示。"})
        if re.search("奶茶|茶饮|饮料|果汁|可乐", free):
            qs.append({"id": f"q{len(qs)+1}", "text": "奶茶或饮料一般是全糖、半糖还是无糖？一周大概几次？"})
        return {"questions": qs[:3]}
    qs = []
    for m in ctx.get("diet_log", {}).get("meals", []):
        text = "；".join(it.get("food", "") for it in m.get("items", []))
        name = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}.get(m["meal"], m["meal"])
        if text and not re.search(f"[{QUANT}]", text):
            qs.append({"id": f"q{len(qs)+1}", "text": f"{name}大概吃了多少？可以用拳头、碗或几个来表示。"})
        if len(qs) >= 2:
            break
    bev = ctx.get("diet_log", {}).get("beverages", [])
    if any(re.search("奶茶|茶饮|饮料|果汁|可乐", b) for b in bev) and len(qs) < 3:
        qs.append({"id": f"q{len(qs)+1}", "text": "奶茶或饮料一般是全糖、半糖还是无糖？一周大概几次？"})
    return {"questions": qs[:3]}


def _advice(ctx: dict) -> dict:
    text = _all_meal_text(ctx)
    conservative = ctx.get("mode") == "conservative"
    cands = []

    if re.search("白粥|稀饭|粥", text):
        cands.append((9, {
            "finding": "早餐以白粥为主，主要是精制碳水",
            "why": "白粥吸收很快，餐后血糖容易升得快，也不太容易饱。",
            "how": "粥里加一把杂豆或燕麦，再配一个鸡蛋和一份青菜，让这一餐更扛饿、更平稳。",
        }))
    if re.search("奶茶|茶饮|饮料|果汁|可乐|甜", text):
        cands.append((10, {
            "finding": "有含糖饮料或奶茶",
            "why": "液体的糖吸收很快，是比较容易被忽视的血糖波动来源。",
            "how": "先从把其中一次换成无糖茶或白水开始，不用一下子全戒掉。",
        }))
    if re.search("面条|米饭|盖饭|馒头|包子|饭", text):
        cands.append((8, {
            "finding": "主食以精制米面为主，蔬菜和蛋白质偏少",
            "why": "精制主食为主、蔬菜少的搭配，餐后血糖更容易波动。",
            "how": "保持你习惯的主食量，把其中一部分换成糙米、杂豆或全谷物；吃饭时先吃蔬菜和蛋白质，再吃主食。" if conservative
            else "把其中三分之一换成糙米、杂豆等粗粮；吃饭时先吃蔬菜和蛋白质，再吃主食。",
        }))
    if not re.search("菜|蔬菜|沙拉|青菜|西兰花|黄瓜|番茄", text):
        cands.append((7, {
            "finding": "这一天里几乎没有看到蔬菜",
            "why": "蔬菜里的膳食纤维有助于让餐后血糖更平稳。",
            "how": "每顿正餐先安排一份蔬菜，大约一个拳头到一碗，外卖也可以加一份青菜。",
        }))
    if re.search("饼干|蛋糕|零食|糕点|薯片|甜点", text):
        cands.append((6, {
            "finding": "晚上有饼干等精制零食",
            "why": "夜间吃精制零食既影响血糖，也容易形成习惯。",
            "how": "换成一小把原味坚果或一杯无糖酸奶，并尽量固定吃的时间。",
        }))
    d = ctx.get("diet_log", {})
    if any(m.get("time", "") and re.match(r"(2[1-3]|0[0-4])", m["time"]) for m in d.get("meals", [])) or "21:00" in str(ctx):
        cands.append((5, {
            "finding": "晚餐时间比较晚",
            "why": "晚餐太晚、间隔太长，容易让血糖和食欲都不稳定。",
            "how": "尽量让晚餐提前一些；确实晚，就少油少炸、控制份量。",
        }))
    generic = [
        (1, {"finding": "进餐的规律性", "why": "每餐食物量和时间比较稳定，血糖更容易平稳。",
             "how": "尽量固定三餐时间，不要长时间空腹后一次吃很多。"}),
        (0, {"finding": "蛋白质的分配", "why": "每餐都有一份蛋白质更容易有饱腹感。",
             "how": "早餐加一个鸡蛋或一杯无糖豆浆；午晚餐各保证一掌心左右的蛋白质食物。"}),
        (0, {"finding": "烹饪方式", "why": "少油少炸有助于控制整体饮食质量。",
             "how": "多选择蒸、煮、炖、快炒，少选油炸和重油重盐的做法。"}),
    ]
    cands.sort(key=lambda x: -x[0])
    items = [c for _, c in cands]
    for _, g in generic:
        if len(items) >= 3:
            break
        items.append(g)
    note = "以上是一般性的生活方式建议，不能替代医生或营养师的评估。"
    if conservative:
        note += " 如果要调整饮食或运动，请先和医生或营养师确认，并按医嘱监测血糖。"
    answers = ctx.get("answers", [])
    return {
        "overview": "（演示模式）根据你填写的这一天饮食，主食偏精制、搭配可以更均衡，下面是最值得先改的三点。",
        "top3": items[:3],
        "good": ["你愿意把一天吃了什么记录下来，这本身就是控糖很重要的第一步。"],
        "missing_info": [] if answers else ["各餐的具体份量和做法没有完全确认，以上有一部分是根据常见情况推测的。"],
        "note": note,
        "sources": [],
    }
