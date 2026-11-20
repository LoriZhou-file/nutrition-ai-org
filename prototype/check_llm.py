"""用真实模型试跑一次，检查输出是否能通过安全校验。

用法（在仓库根目录）：
    python -m prototype.check_llm            # 标准模式用例
    python -m prototype.check_llm conservative
需要先在 .env 里配置 DEEPSEEK_API_KEY；没配置则是演示模式。
"""
import json
import sys

from . import guard, knowledge, llm, prompts

CASES = {
    "standard": {
        "mode": "standard",
        "diet_log": {"meals": [
            {"meal": "breakfast", "items": [{"food": "白粥一碗"}, {"food": "咸菜"}, {"food": "一根油条"}]},
            {"meal": "lunch", "items": [{"food": "外卖盖饭，米饭满满一盒"}, {"food": "一点青菜"}]},
            {"meal": "dinner", "time": "21:30", "items": [{"food": "面条一大碗"}, {"food": "几片肉"}]},
            {"meal": "snack", "items": [{"food": "一杯奶茶"}, {"food": "饼干几块"}]}],
            "beverages": ["甜茶饮一瓶"]},
    },
    "conservative": {
        "mode": "conservative",
        "diet_log": {"meals": [
            {"meal": "breakfast", "items": [{"food": "不吃，只喝一杯咖啡"}]},
            {"meal": "lunch", "items": [{"food": "一小碗米饭"}, {"food": "几口菜"}]},
            {"meal": "dinner", "items": [{"food": "一个馒头"}, {"food": "一点青菜"}]}],
            "beverages": []},
    },
}


def main():
    key = sys.argv[1] if len(sys.argv) > 1 else "standard"
    view = CASES[key]
    print("引擎：", llm.engine_name(), "| 模式：", view["mode"])
    units = knowledge.retrieve(json.dumps(view["diet_log"], ensure_ascii=False))
    print("检索到知识：", [u["id"] for u in units])
    out = llm.chat_json(
        prompts.system_prompt(view["mode"], "advice", units),
        prompts.user_prompt(view, [], []),
        "advice",
        {"diet_log": view["diet_log"], "mode": view["mode"], "answers": []},
    )
    out = guard.ensure_note(out, view["mode"])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    v = guard.check(out, view["mode"])
    print("\n安全校验：", "通过" if not v else v)


if __name__ == "__main__":
    main()
