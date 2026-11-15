"""出账范围：本期采用哪一份运价、哪一批运单，以及每票最终取自哪次补发。

范围只由“已登记的来源”决定，读库时按登记顺序解析，因此同一组来源永远得到同一个
basis_digest；补发重复登记或重复构建都不会改变范围，也不会新增版本。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app import config, manifest, store
from app.modules.m2_rates import version as rateversion


def period_of(pickup_utc: str) -> str:
    """提货时刻按 Asia/Shanghai 落月；无法解析时返回空串。"""
    stamp = rateversion.parse_utc(pickup_utc)
    if stamp is None:
        return ""
    return stamp.astimezone(config.CN_TZ).strftime("%Y-%m")


def valid_period(period: str) -> bool:
    if len(period) != 7 or period[4] != "-":
        return False
    year, month = period[:4], period[5:]
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


@dataclass
class Scope:
    """一次构建所用的完整范围。"""

    period: str
    rate_sources: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    waybills: list[dict] = field(default_factory=list)
    superseded: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    quarantine: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    carrier_source: dict[str, str] = field(default_factory=dict)

    @property
    def rate_source(self) -> dict | None:
        return self.rate_sources[-1] if self.rate_sources else None

    @property
    def ready(self) -> bool:
        return bool(self.rate_sources) and bool(self.sources)

    def summary(self) -> dict:
        return {
            "period": self.period,
            "rate_source": self.rate_source,
            "rate_sources": self.rate_sources,
            "carrier_sources": self.carrier_source,
            "rate_card_count": len(self.cards),
            "rate_set_version": max((int(c["rate_set_version"]) for c in self.cards), default=0),
            "waybills": len(self.waybills),
            "superseded": len(self.superseded),
            "excluded": len(self.excluded),
            "quarantine": len(self.quarantine),
            "sources": self.sources,
        }


CARD_DIGEST_FIELDS = (
    "carrier", "service", "zone", "version_id", "effective_from", "effective_to",
    "first_weight_kg", "first_price", "step_weight_kg", "step_price", "min_charge",
    "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct", "currency",
    "rate_set_version",
)

BILL_FIELDS = (
    "waybill_no", "carrier", "service", "zone", "pickup_utc", "weight_kg",
    "length_cm", "width_cm", "height_cm", "declared_value", "pieces",
)


def resolve(period: str) -> Scope:
    """按登记顺序解析本期范围。

    运价按承运商合并：后登记的补发只替换它覆盖到的承运商，其它承运商沿用先前来源。
    承运商只在某一来源里出现一次，避免某家只补自己那半张表时把别家价格整份冲掉。
    运单按批次后到优先，同号以更晚的补发为准。
    """
    sources = store.close_sources(period)
    rate_sources = [s for s in sources if s["kind"] == "rate"]
    cards, carrier_source = _merge_rate_sources(period, rate_sources)

    selected: dict[str, dict] = {}
    superseded: list[dict] = []
    excluded: list[dict] = []
    quarantine: list[dict] = []

    waybill_sources = [s for s in sources if s["kind"] == "waybill"]
    for source in waybill_sources:
        rows = store.waybills(source["run_id"]) if source["run_id"] else []
        for row in rows:
            if period_of(row["pickup_utc"]) != period:
                excluded.append({
                    "waybill_no": row["waybill_no"],
                    "run_id": source["run_id"],
                    "input_name": source["input_name"],
                    "line_no": row["line_no"],
                    "reason": "PERIOD_OUT_OF_SCOPE",
                    "pickup_utc": row["pickup_utc"],
                })
                continue
            previous = selected.get(row["waybill_no"])
            if previous is not None:
                changed = [f for f in BILL_FIELDS if str(previous.get(f)) != str(row.get(f))]
                if changed:
                    superseded.append({
                        "waybill_no": row["waybill_no"],
                        "from_run_id": previous["source_run_id"],
                        "from_input_name": previous["source_input_name"],
                        "from_line_no": previous["source_line_no"],
                        "to_run_id": source["run_id"],
                        "to_input_name": source["input_name"],
                        "to_line_no": row["line_no"],
                        "changed_fields": ",".join(changed),
                    })
            selected[row["waybill_no"]] = {
                **{f: row.get(f, "") for f in BILL_FIELDS},
                "source_run_id": source["run_id"],
                "source_input_name": source["input_name"],
                "source_line_no": int(row["line_no"]),
            }
        for item in store.quarantine(source["run_id"]):
            quarantine.append({
                "waybill_no": item["waybill_no"],
                "run_id": source["run_id"],
                "input_name": source["input_name"],
                "line_no": item["line_no"],
                "code": item["code"],
                "detail": item["detail"],
            })

    waybills = [selected[key] for key in sorted(selected)]
    return Scope(
        period=period,
        rate_sources=rate_sources,
        cards=cards,
        waybills=waybills,
        superseded=superseded,
        excluded=excluded,
        quarantine=quarantine,
        sources=sources,
        carrier_source=carrier_source,
    )


def _merge_rate_sources(period: str, rate_sources: list[dict]) -> tuple[list[dict], dict]:
    """逐来源覆盖承运商：后登记者优先，未覆盖的承运商保留先前版本。"""
    by_carrier: dict[str, list[dict]] = {}
    owner: dict[str, str] = {}
    for source in rate_sources:
        for card in store.close_rate_cards(period, source["source_id"]):
            carrier = card["carrier"]
            if owner.get(carrier) != source["source_id"]:
                by_carrier[carrier] = []
                owner[carrier] = source["source_id"]
            by_carrier[carrier].append(card)
    merged: list[dict] = []
    for carrier in sorted(by_carrier):
        merged.extend(by_carrier[carrier])
    return merged, dict(sorted(owner.items()))


def _canonical_cards(cards: list[dict]) -> list[dict]:
    return [
        {field: str(card.get(field, "")) for field in CARD_DIGEST_FIELDS}
        for card in sorted(
            cards,
            key=lambda c: (c["carrier"], c["service"], c["zone"], c["effective_from"],
                           c["version_id"]),
        )
    ]


def _canonical_waybills(waybills: list[dict]) -> list[dict]:
    return [
        {
            **{field: str(row.get(field, "")) for field in BILL_FIELDS},
            "source_run_id": row["source_run_id"],
            "source_line_no": int(row["source_line_no"]),
        }
        for row in sorted(waybills, key=lambda r: r["waybill_no"])
    ]


def basis_payload(scope: Scope) -> dict:
    """范围指纹只包含会影响账单身份的内容。

    证据行的身份（隔离行、超范围行、被覆盖事件）也纳入，保证同一版本自带的证据
    与账单一一对应；同一份补发重复登记时这些内容完全一致，因此指纹不变。
    """
    return {
        "period": scope.period,
        "rules_version": config.RULES_VERSION,
        "profile": config.PROFILE,
        # 合并后的运价卡已完整表达版本身份；带上每家承运商取自哪个来源便于审计
        "carrier_sources": scope.carrier_source,
        "rate_cards": _canonical_cards(scope.cards),
        "waybills": _canonical_waybills(scope.waybills),
        "superseded": sorted(
            [[e["waybill_no"], e["changed_fields"], e["from_run_id"], e["to_run_id"]]
             for e in scope.superseded]
        ),
        "excluded": sorted(
            [[e["waybill_no"], e["run_id"], int(e["line_no"])] for e in scope.excluded]
        ),
        "quarantine": sorted(
            [[q["run_id"], int(q["line_no"]), q["code"]] for q in scope.quarantine]
        ),
    }


def basis_digest(scope: Scope) -> str:
    payload = manifest.dumps(basis_payload(scope))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rate_digest(cards: list[dict]) -> str:
    payload = manifest.dumps(_canonical_cards(cards))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
