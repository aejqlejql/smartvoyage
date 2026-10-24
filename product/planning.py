"""Deterministic sandbox supplier and planner. Never presents sample data as live facts."""

import math
import time
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .store import Problem

DESTINATIONS = {
    "成都": {
        "spots": [
            "人民公园与宽窄巷子",
            "成都博物馆与文殊院",
            "东郊记忆与城市漫步",
            "锦江滨水步行",
            "浣花溪周边漫步",
            "社区书店与休息",
            "伴手礼采购与自由活动",
        ],
        "hotel": 24000,
    },
    "杭州": {
        "spots": [
            "西湖步行与湖滨",
            "博物馆与京杭运河周边",
            "钱塘江与城市漫步",
            "城市书店与休息",
            "老街与文化街区",
            "湖边慢行与摄影",
            "伴手礼采购与自由活动",
        ],
        "hotel": 26000,
    },
    "西安": {
        "spots": [
            "城墙周边与书院门",
            "历史街区与大雁塔周边",
            "回民街与城市漫步",
            "文化街区与城市书店",
            "博物馆主题探索",
            "公园漫步与休息",
            "伴手礼采购与自由活动",
        ],
        "hotel": 22000,
    },
}
# Explicit sample catalogue: prices are fabricated for software acceptance, not actual fares.
ROUTES = {
    ("北京", "成都"): (55000, 85000),
    ("上海", "成都"): (65000, 95000),
    ("广州", "成都"): (50000, 75000),
    ("北京", "杭州"): (60000, 80000),
    ("上海", "杭州"): (8000, 50000),
    ("广州", "杭州"): (70000, 85000),
    ("北京", "西安"): (50000, 70000),
    ("上海", "西安"): (65000, 85000),
    ("广州", "西安"): (75000, 90000),
}


def local_today():
    # Asia/Shanghai without depending on the host timezone or Windows tzdata.
    from datetime import datetime, timezone

    return datetime.now(timezone(timedelta(hours=8))).date()


class TripRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(min_length=2, max_length=20)
    destination: str = Field(min_length=2, max_length=20)
    departure_date: date
    days: int = Field(default=3, ge=2, le=7)
    travelers: int = Field(default=1, ge=1, le=6)
    budget_cents: int = Field(default=300000, ge=10000, le=100000000)
    transport: str = "train"
    include_insurance: bool = True
    notes: str = Field(default="", max_length=500)

    @field_validator("transport")
    @classmethod
    def valid_transport(cls, value):
        if value not in {"train", "flight"}:
            raise ValueError("交通方式只支持 train 或 flight")
        return value

    @model_validator(mode="after")
    def valid_trip(self):
        if (self.origin, self.destination) not in ROUTES:
            raise ValueError("演示支持北京/上海/广州出发，前往成都/杭州/西安的九条线路")
        if not local_today() <= self.departure_date <= local_today() + timedelta(days=180):
            raise ValueError("出发日期须在今天至未来180天内")
        return self


def build_quote(request: TripRequest):
    route = ROUTES[(request.origin, request.destination)]
    fare = route[0 if request.transport == "train" else 1]
    transport_label = "高铁二等座" if request.transport == "train" else "经济舱机票"
    items = []

    def item(product_id, label, service_date, price, quantity, capacity):
        items.append(
            {
                "product_id": product_id,
                "label": label,
                "service_date": str(service_date),
                "unit_price_cents": price,
                "quantity": quantity,
                "subtotal_cents": price * quantity,
                "capacity": capacity,
                "source": "sandbox_catalog_v1",
            }
        )

    for origin, destination, when in [
        (request.origin, request.destination, request.departure_date),
        (request.destination, request.origin, request.departure_date + timedelta(days=request.days - 1)),
    ]:
        item(
            f"{request.transport}:{origin}:{destination}",
            f"{origin} → {destination} · {transport_label}",
            when,
            fare,
            request.travelers,
            20,
        )
    for day in range(request.days - 1):
        item(
            f"hotel:{request.destination}",
            f"{request.destination} · 示例舒适双床房（每间住2人）",
            request.departure_date + timedelta(days=day),
            DESTINATIONS[request.destination]["hotel"],
            math.ceil(request.travelers / 2),
            10,
        )
    if request.include_insurance:
        item(
            "insurance:basic",
            "示例旅行保障（模拟，不构成保险合同）",
            request.departure_date,
            3000,
            request.travelers,
            1000,
        )

    total = sum(row["subtotal_cents"] for row in items)
    # Food/local mobility are explicit estimates, excluded from the payable order amount.
    local_estimate = request.days * request.travelers * 12000
    if total + local_estimate > request.budget_cents:
        raise Problem(
            "budget_exceeded",
            f"该方案预订项¥{total / 100:.2f}，餐饮及市内交通预估¥{local_estimate / 100:.2f}，"
            f"超过总预算¥{request.budget_cents / 100:.2f}。请调整天数、人数、交通方式或预算。",
            422,
        )
    spots = DESTINATIONS[request.destination]["spots"]
    itinerary = [
        {
            "day": i + 1,
            "date": str(request.departure_date + timedelta(days=i)),
            "title": "抵达与轻松游览" if i == 0 else ("自由活动与返程" if i == request.days - 1 else "城市探索"),
            "activity": spots[i % len(spots)],
            "note": "示例路线，景点开放时间、门票与交通班次需另行核实",
        }
        for i in range(request.days)
    ]
    return {
        "currency": "CNY",
        "source": "sandbox_catalog_v1",
        "simulation": True,
        "items": items,
        "amount_cents": total,
        "local_estimate_cents": local_estimate,
        "estimated_total_cents": total + local_estimate,
        "budget_cents": request.budget_cents,
        "itinerary": itinerary,
        "quoted_at": int(time.time()),
        "warnings": [
            "所有价格、产品和库存均为演示数据；不会真实出票、订房、扣款或投保。",
            "天气未查询，不能作为真实天气预报；出发前请核实天气及景点开放情况。",
            "订单包含往返交通、住宿及所选模拟保障；餐饮、市内交通、景点门票不在订单中。",
            "未提供交通班次和到离时间；每日安排不代表已经通过真实时刻表校验。",
        ],
    }
