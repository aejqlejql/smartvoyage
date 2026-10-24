"""Export a saved plan without introducing document rendering dependencies."""


def plan_markdown(plan):
    q = plan["quote"]
    lines = [
        f"# {plan['title']}",
        "",
        "> 个人项目演示：所有产品、价格及交易均为模拟。",
        "",
        f"出发日期：{plan['request']['departure_date']}，方案版本：{plan['version']}",
        "",
        "## 每日安排",
        "",
    ]
    for day in q["itinerary"]:
        lines.extend(
            [f"### 第{day['day']}天 · {day['date']} · {day['title']}", "", day["activity"], "", day["note"], ""]
        )
    lines.extend(["## 模拟费用", "", "| 项目 | 日期 | 数量 | 小计 |", "|---|---|---:|---:|"])
    for item in q["items"]:
        lines.append(
            f"| {item['label']} | {item['service_date']} | {item['quantity']} | ¥{item['subtotal_cents'] / 100:.2f} |"
        )
    lines.extend(
        [
            "",
            f"模拟预订金额：¥{q['amount_cents'] / 100:.2f}",
            "",
            f"餐饮及市内交通预估（不支付）：¥{q['local_estimate_cents'] / 100:.2f}",
            "",
            f"旅行合计预估：¥{q['estimated_total_cents'] / 100:.2f}",
            "",
            "## 说明",
            "",
        ]
    )
    lines.extend(f"- {warning}" for warning in q["warnings"])
    if plan["request"]["notes"]:
        # Fence user content to preserve its literal form, even if it contains markdown.
        lines.extend(["", "## 个人备注", "", *["> " + line for line in plan["request"]["notes"].splitlines()]])
    return "\n".join(lines) + "\n"
