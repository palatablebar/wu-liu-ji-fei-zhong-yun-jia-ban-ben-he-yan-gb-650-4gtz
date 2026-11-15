"""M5 编排：按运单组合分区价与附加费，落库账单行并回写运行合计。"""

from __future__ import annotations

from decimal import Decimal

from app import manifest, store
from app.errors import DomainError
from app.modules.m2_rates import service as rates
from app.modules.m2_rates import version as rateversion
from app.modules.m4_weights import billable
from app.modules.m5_charges import addon, money, zone
from app.reasons import sort_codes


def price_charge(declared_value, steps: int, card: dict) -> dict:
    """纯计价：给定声明价值、段数与已选版本，算出五个分项与应收。

    M5 预览与 M8 正式账单共用这一份规则，避免两处各算一套。
    """
    first = zone.first_amount(card)
    step_part = zone.step_amount(steps, card)
    topup = zone.min_topup(first + step_part, card)
    base = money.q2(first + topup)
    remote = addon.remote_amount(card)
    fuel = addon.fuel_amount(base, step_part, card)
    insurance = addon.insurance_amount(Decimal(declared_value), card)
    amount = money.total([base, step_part, remote, fuel, insurance])
    codes: list[str] = []
    if topup > 0:
        codes.append("MIN_CHARGE_APPLIED")
    if remote > 0:
        codes.append("ZONE_REMOTE")
    return {
        "base_amount": str(base),
        "step_amount": str(step_part),
        "remote_amount": str(remote),
        "fuel_amount": str(fuel),
        "insurance_amount": str(insurance),
        "total_amount": str(amount),
        "codes": codes,
        "topup": topup,
    }


def compute_charges(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise DomainError("RUN_NOT_FOUND", f"运行不存在：{run_id}", 404)
    weights = store.weight_lines(run_id)
    if not weights:
        raise DomainError("NO_WEIGHTS", "该运行还没有计费重", 409)
    waybills = {row["waybill_no"]: row for row in store.waybills(run_id)}
    index_map = rates.index()

    lines: list[dict] = []
    for weight in weights:
        if weight["source"] == billable.UNBILLED:
            continue
        waybill = waybills.get(weight["waybill_no"])
        if waybill is None:
            continue
        pickup = rateversion.parse_utc(waybill["pickup_utc"])
        card, codes = rates.select_card(
            index_map, waybill["carrier"], waybill["service"], waybill["zone"], pickup
        )
        if card is None:
            continue
        priced = price_charge(waybill["declared_value"], int(weight["steps"]), card)

        row_codes = list(codes) + list(priced["codes"])
        if weight["source"] == billable.VOLUMETRIC:
            row_codes.append("VOLUMETRIC_APPLIED")

        lines.append({
            "waybill_no": weight["waybill_no"],
            "version_id": weight["version_id"],
            "base_amount": priced["base_amount"],
            "step_amount": priced["step_amount"],
            "remote_amount": priced["remote_amount"],
            "fuel_amount": priced["fuel_amount"],
            "insurance_amount": priced["insurance_amount"],
            "total_amount": priced["total_amount"],
            "codes": ",".join(sort_codes(row_codes)),
        })

    store.replace_charge_lines(run_id, lines)
    grand = money.total([Decimal(line["total_amount"]) for line in lines])
    now = manifest.utc_now_iso()
    store.upsert_run(run_id, {"amount_total": str(grand), "updated_at": now})
    store.add_stage(run_id, "charges", now)
    return {
        "run_id": run_id,
        "billed": len(lines),
        "amount_total": str(grand),
        "rate_digest": rates.rate_digest(),
    }


def lines(run_id: str) -> list[dict]:
    return store.charge_lines(run_id) if run_id else []


def amounts(run_id: str) -> dict:
    items = lines(run_id)
    parts = {
        "base": money.total([Decimal(i["base_amount"]) for i in items]),
        "step": money.total([Decimal(i["step_amount"]) for i in items]),
        "remote": money.total([Decimal(i["remote_amount"]) for i in items]),
        "fuel": money.total([Decimal(i["fuel_amount"]) for i in items]),
        "insurance": money.total([Decimal(i["insurance_amount"]) for i in items]),
    }
    grand = money.total([Decimal(i["total_amount"]) for i in items])
    return {"count": len(items), "parts": parts, "total": grand,
            "conserved": money.conserved(grand, parts.values())}
