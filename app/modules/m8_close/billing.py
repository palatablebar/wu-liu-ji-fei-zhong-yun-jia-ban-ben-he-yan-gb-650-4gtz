"""账单快照构建：把范围算成一份完整、自洽、可复算的版本快照。

计价规则不在这里重写：计费重复用 M4 的 price_weight，分项复用 M5 的 price_charge。
本文件只负责“按本期选定的运价与运单逐票结算”，并把结果连同一份证据一起定稿。
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from app import config, manifest
from app.modules.m2_rates import ratecard, version as rateversion
from app.modules.m4_weights import billable, service as weight_service
from app.modules.m5_charges import service as charge_service
from app.modules.m6_reconcile import totals
from app.modules.m8_close import scope as scope_mod

PART_KEYS = ("base", "step", "remote", "fuel", "insurance")
PART_FIELDS = {
    "base": "base_amount",
    "step": "step_amount",
    "remote": "remote_amount",
    "fuel": "fuel_amount",
    "insurance": "insurance_amount",
}
PART_LABELS = {
    "base": "基础运费",
    "step": "续重运费",
    "remote": "偏远附加",
    "fuel": "燃油附加",
    "insurance": "保价费",
}


def price_waybill(row: dict, card: dict) -> dict:
    priced = weight_service.price_weight(row, card)
    charge = charge_service.price_charge(row["declared_value"], priced["steps"], card)
    codes = list(priced["codes"]) + list(charge["codes"])
    if priced["source"] == billable.VOLUMETRIC:
        codes.append("VOLUMETRIC_APPLIED")
    return {
        "waybill_no": row["waybill_no"],
        "carrier": row["carrier"],
        "service": row["service"],
        "zone": row["zone"],
        "pickup_utc": row["pickup_utc"],
        "actual_kg": priced["actual_kg"],
        "volumetric_kg": priced["volumetric_kg"],
        "weight_source": priced["source"],
        "billable_kg": priced["billable_kg"],
        "steps": int(priced["steps"]),
        "rate_version_id": card["version_id"],
        "rate_set_version": int(card["rate_set_version"]),
        "effective_from": card["effective_from"],
        "effective_to": card["effective_to"],
        "first_weight_kg": card["first_weight_kg"],
        "first_price": card["first_price"],
        "step_weight_kg": card["step_weight_kg"],
        "step_price": card["step_price"],
        "min_charge": card["min_charge"],
        "volume_divisor": int(card["volume_divisor"]),
        "fuel_pct": card["fuel_pct"],
        "remote_fee": card["remote_fee"],
        "insurance_pct": card["insurance_pct"],
        "currency": card["currency"],
        "base_amount": charge["base_amount"],
        "step_amount": charge["step_amount"],
        "remote_amount": charge["remote_amount"],
        "fuel_amount": charge["fuel_amount"],
        "insurance_amount": charge["insurance_amount"],
        "total_amount": charge["total_amount"],
        "source_run_id": row["source_run_id"],
        "source_input_name": row["source_input_name"],
        "source_line_no": int(row["source_line_no"]),
        "codes": codes,
    }


def price_scope(scope: scope_mod.Scope) -> tuple[list[dict], list[dict]]:
    """逐票结算；返回 (计费行, 未计费行)。未计费只指运价无命中或重叠。"""
    index = ratecard.index_by_key(scope.cards)
    invoice: list[dict] = []
    unbilled: list[dict] = []
    for row in scope.waybills:
        pickup = rateversion.parse_utc(row["pickup_utc"])
        card, codes = rateversion.select(index.get(
            (row["carrier"], row["service"], row["zone"]), []), pickup)
        if card is None:
            unbilled.append({
                "waybill_no": row["waybill_no"],
                "carrier": row["carrier"],
                "service": row["service"],
                "zone": row["zone"],
                "pickup_utc": row["pickup_utc"],
                "run_id": row["source_run_id"],
                "input_name": row["source_input_name"],
                "line_no": int(row["source_line_no"]),
                "code": codes[0] if codes else "RATE_MISSING",
                "detail": ",".join(codes),
            })
            continue
        priced = price_waybill(row, card)
        priced["codes"] = ",".join(sorted(set(codes + priced["codes"])))
        invoice.append(priced)
    return invoice, unbilled


def invoice_totals(invoice: list[dict]) -> dict:
    rows = [
        {
            "base_amount": line["base_amount"],
            "step_amount": line["step_amount"],
            "remote_amount": line["remote_amount"],
            "fuel_amount": line["fuel_amount"],
            "insurance_amount": line["insurance_amount"],
            "total_amount": line["total_amount"],
        }
        for line in invoice
    ]
    parts = totals.parts_of(rows)
    grand = totals.grand_total(rows)
    return {
        "parts": {key: str(value) for key, value in parts.items()},
        "total": str(grand),
        "conserved": bool(totals.conserved(rows)),
    }


# --- 与基线版本的差异 -------------------------------------------------------

def _line_view(line: dict) -> dict:
    return {
        "billable_kg": str(line.get("billable_kg", "")),
        "rate_version_id": line.get("rate_version_id", ""),
        "rate_set_version": str(line.get("rate_set_version", "")),
        "total": str(line.get("total_amount", "0.00")),
        "parts": {
            key: str(line.get(PART_FIELDS[key], "0.00")) for key in PART_KEYS
        },
    }


def _rate_moved(old: dict, new: dict) -> bool:
    """运价版本号或运价集版本任一变化都算换过价；补发只改单价也会命中。"""
    return (
        old["rate_version_id"] != new["rate_version_id"]
        or old["rate_set_version"] != new["rate_set_version"]
    )


def _impact_entry(waybill_no: str, old: dict | None, new: dict | None) -> dict | None:
    old_view = _line_view(old) if old else None
    new_view = _line_view(new) if new else None

    if old_view is None and new_view is None:
        return None
    if old_view is None:
        return {
            "waybill_no": waybill_no, "change": "added",
            "rate_changed": True, "weight_changed": True, "amount_changed": True,
            "old_billable_kg": "", "new_billable_kg": new_view["billable_kg"],
            "old_version_id": "", "new_version_id": new_view["rate_version_id"],
            "old_rate_set_version": 0,
            "new_rate_set_version": int(new_view["rate_set_version"] or 0),
            "old_total": "0.00", "new_total": new_view["total"],
            "delta_total": new_view["total"], "delta_billable_kg": new_view["billable_kg"],
            "changed_parts": ",".join(PART_KEYS),
            "delta_parts": {key: new_view["parts"][key] for key in PART_KEYS},
        }
    if new_view is None:
        return {
            "waybill_no": waybill_no, "change": "removed",
            "rate_changed": True, "weight_changed": True, "amount_changed": True,
            "old_billable_kg": old_view["billable_kg"], "new_billable_kg": "",
            "old_version_id": old_view["rate_version_id"], "new_version_id": "",
            "old_rate_set_version": int(old_view["rate_set_version"] or 0),
            "new_rate_set_version": 0,
            "old_total": old_view["total"], "new_total": "0.00",
            "delta_total": str(-Decimal(old_view["total"])),
            "delta_billable_kg": str(-Decimal(old_view["billable_kg"])),
            "changed_parts": ",".join(PART_KEYS),
            "delta_parts": {key: str(-Decimal(old_view["parts"][key])) for key in PART_KEYS},
        }

    changed_parts = [
        key for key in PART_KEYS if old_view["parts"][key] != new_view["parts"][key]
    ]
    rate_changed = _rate_moved(old_view, new_view)
    weight_changed = Decimal(old_view["billable_kg"]) != Decimal(new_view["billable_kg"])
    amount_changed = Decimal(old_view["total"]) != Decimal(new_view["total"])
    if not (changed_parts or rate_changed or weight_changed or amount_changed):
        return None
    delta_parts = {
        key: str(Decimal(new_view["parts"][key]) - Decimal(old_view["parts"][key]))
        for key in PART_KEYS
    }
    return {
        "waybill_no": waybill_no, "change": "changed",
        "rate_changed": rate_changed, "weight_changed": weight_changed,
        "amount_changed": amount_changed,
        "old_billable_kg": old_view["billable_kg"], "new_billable_kg": new_view["billable_kg"],
        "old_version_id": old_view["rate_version_id"],
        "new_version_id": new_view["rate_version_id"],
        "old_rate_set_version": int(old_view["rate_set_version"] or 0),
        "new_rate_set_version": int(new_view["rate_set_version"] or 0),
        "old_total": old_view["total"], "new_total": new_view["total"],
        "delta_total": str(Decimal(new_view["total"]) - Decimal(old_view["total"])),
        "delta_billable_kg": str(Decimal(new_view["billable_kg"]) - Decimal(old_view["billable_kg"])),
        "changed_parts": ",".join(changed_parts),
        "delta_parts": delta_parts,
    }


def compute_impacts(invoice: list[dict], baseline_invoice: list[dict],
                    scope: scope_mod.Scope,
                    baseline_scope: dict | None) -> dict:
    """对比上一正式版本，列出被补发改变的账单与范围事件。"""
    old_map = {line["waybill_no"]: line for line in baseline_invoice}
    new_map = {line["waybill_no"]: line for line in invoice}
    entries: list[dict] = []
    unchanged = 0
    for waybill_no in sorted(set(old_map) | set(new_map)):
        entry = _impact_entry(waybill_no, old_map.get(waybill_no), new_map.get(waybill_no))
        if entry is None:
            unchanged += 1
            continue
        entries.append(entry)

    baseline_scope = baseline_scope or {}
    quarantine_new = _scope_diff(
        scope.quarantine, baseline_scope.get("quarantined", []),
        key=lambda q: (q["run_id"], int(q["line_no"]), q["code"]),
    )
    excluded_new = _scope_diff(
        scope.excluded, baseline_scope.get("excluded", []),
        key=lambda e: (e["run_id"], int(e["line_no"]), e["waybill_no"]),
    )
    superseded_new = _scope_diff(
        scope.superseded, baseline_scope.get("superseded", []),
        key=lambda s: (s["waybill_no"], s["from_run_id"], s["to_run_id"], s["changed_fields"]),
    )

    return {
        "entries": entries,
        "added": [e["waybill_no"] for e in entries if e["change"] == "added"],
        "removed": [e["waybill_no"] for e in entries if e["change"] == "removed"],
        "changed": [e["waybill_no"] for e in entries if e["change"] == "changed"],
        "unchanged": unchanged,
        "new_quarantine": quarantine_new,
        "new_excluded": excluded_new,
        "new_superseded": superseded_new,
        "delta_total": str(sum(
            (Decimal(e["delta_total"]) for e in entries), Decimal("0.00")
        )),
        "delta_billable_kg": str(sum(
            (Decimal(e["delta_billable_kg"]) for e in entries), Decimal("0.000")
        )),
    }


def _scope_diff(current: list[dict], baseline: list[dict], key) -> list[dict]:
    known = {key(item) for item in baseline}
    return [item for item in current if key(item) not in known]


def impact_payload(impacts: dict, scope: scope_mod.Scope) -> dict:
    """影响指纹：账单差异 + 范围事件。复核一旦绑定指纹，数据再变就必须重新复核。"""
    return {
        "entries": [
            [
                e["waybill_no"], e["change"], int(e["rate_changed"]),
                int(e["weight_changed"]), int(e["amount_changed"]),
                e["old_billable_kg"], e["new_billable_kg"],
                e["old_version_id"], e["new_version_id"],
                e.get("old_rate_set_version", 0), e.get("new_rate_set_version", 0),
                e["old_total"], e["new_total"], e["delta_total"],
                e["changed_parts"], e["delta_parts"],
            ]
            for e in impacts["entries"]
        ],
        "quarantine": sorted(
            [[q["run_id"], int(q["line_no"]), q["code"]] for q in scope.quarantine]
        ),
        "excluded": sorted(
            [[e["run_id"], int(e["line_no"]), e["waybill_no"]] for e in scope.excluded]
        ),
        "superseded": sorted(
            [s["waybill_no"], s["from_run_id"], s["to_run_id"], s["changed_fields"]]
            for s in scope.superseded
        ),
    }


def impact_digest(impacts: dict, scope: scope_mod.Scope) -> str:
    payload = manifest.dumps(impact_payload(impacts, scope))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- 证据与对账 -------------------------------------------------------------

def build_evidence(invoice: list[dict], unbilled: list[dict],
                   scope: scope_mod.Scope) -> list[dict]:
    evidence: list[dict] = []
    for line in invoice:
        evidence.append({
            "row_kind": "invoice",
            "waybill_no": line["waybill_no"],
            "payload": manifest.dumps({
                "kind": "invoice",
                "waybill_no": line["waybill_no"],
                "carrier": line["carrier"],
                "service": line["service"],
                "zone": line["zone"],
                "pickup_utc": line["pickup_utc"],
                "actual_kg": line["actual_kg"],
                "volumetric_kg": line["volumetric_kg"],
                "weight_source": line["weight_source"],
                "billable_kg": line["billable_kg"],
                "steps": line["steps"],
                "rate_version_id": line["rate_version_id"],
                "rate_set_version": line["rate_set_version"],
                "effective_from": line["effective_from"],
                "effective_to": line["effective_to"],
                "first_price": line["first_price"],
                "step_price": line["step_price"],
                "min_charge": line["min_charge"],
                "fuel_pct": line["fuel_pct"],
                "remote_fee": line["remote_fee"],
                "insurance_pct": line["insurance_pct"],
                "parts": {key: line[PART_FIELDS[key]] for key in PART_KEYS},
                "total_amount": line["total_amount"],
                "currency": line["currency"],
                "source_run_id": line["source_run_id"],
                "source_input_name": line["source_input_name"],
                "source_line_no": line["source_line_no"],
                "codes": line["codes"],
            }),
        })
    for item in unbilled:
        evidence.append({
            "row_kind": "unbilled",
            "waybill_no": item["waybill_no"],
            "payload": manifest.dumps({"kind": "unbilled", **item}),
        })
    for item in sorted(scope.quarantine, key=lambda q: (q["run_id"], q["line_no"], q["code"])):
        evidence.append({
            "row_kind": "quarantine",
            "waybill_no": item["waybill_no"],
            "payload": manifest.dumps({"kind": "quarantine", **item}),
        })
    for item in sorted(scope.excluded, key=lambda e: (e["run_id"], e["line_no"])):
        evidence.append({
            "row_kind": "excluded",
            "waybill_no": item["waybill_no"],
            "payload": manifest.dumps({"kind": "excluded", **item}),
        })
    for item in sorted(scope.superseded, key=lambda s: (s["waybill_no"], s["to_run_id"])):
        evidence.append({
            "row_kind": "superseded",
            "waybill_no": item["waybill_no"],
            "payload": manifest.dumps({"kind": "superseded", **item}),
        })
    return evidence


def build_scope_rows(invoice: list[dict], unbilled: list[dict],
                     scope: scope_mod.Scope) -> dict:
    return {
        "billed": [
            {
                "waybill_no": line["waybill_no"],
                "run_id": line["source_run_id"],
                "line_no": line["source_line_no"],
                "payload": manifest.dumps({
                    "source_input_name": line["source_input_name"],
                    "billable_kg": line["billable_kg"],
                    "total_amount": line["total_amount"],
                }),
            }
            for line in invoice
        ],
        "unbilled": [
            {
                "waybill_no": item["waybill_no"],
                "run_id": item["run_id"],
                "line_no": item["line_no"],
                "code": item["code"],
                "detail": item["detail"],
                "payload": manifest.dumps(item),
            }
            for item in unbilled
        ],
        "quarantined": [
            {
                "waybill_no": item["waybill_no"],
                "run_id": item["run_id"],
                "line_no": item["line_no"],
                "code": item["code"],
                "detail": item["detail"],
                "payload": manifest.dumps(item),
            }
            for item in scope.quarantine
        ],
        "excluded": [
            {
                "waybill_no": item["waybill_no"],
                "run_id": item["run_id"],
                "line_no": item["line_no"],
                "code": item["reason"],
                "detail": item["input_name"],
                "payload": manifest.dumps(item),
            }
            for item in scope.excluded
        ],
        "superseded": [
            {
                "waybill_no": item["waybill_no"],
                "run_id": item["to_run_id"],
                "line_no": item["to_line_no"],
                "code": "RESEND_SUPERSEDED",
                "detail": item["changed_fields"],
                "payload": manifest.dumps(item),
            }
            for item in scope.superseded
        ],
    }


def build_accounts(scope: scope_mod.Scope, invoice: list[dict],
                   unbilled: list[dict], totals_info: dict) -> list[dict]:
    """关账口径的四账。左侧为本期来源里的运单行，右侧为下游拆分，任一侧不等都能定位。

    口径：本期来源中提货时刻落在本期的每一行，最终只有三种去向——计费、未计费、
    被更晚的补发覆盖。隔离行从未进入采信表、超范围行属于别的月份，两者单列在版本
    清单里，不混进本期的运单行账。
    """
    period_rows = len(invoice) + len(unbilled) + len(scope.superseded)
    row_parts = [
        {"label": "计费", "value": len(invoice)},
        {"label": "未计费", "value": len(unbilled)},
        {"label": "被补发覆盖", "value": len(scope.superseded)},
    ]
    rows_account = _account("rows", "运单行账", "本期运单行", period_rows, row_parts, "行")

    amount_parts = [
        {"label": PART_LABELS[key], "value": Decimal(totals_info["parts"][key])}
        for key in PART_KEYS
    ]
    amount_account = _account(
        "amount", "金额账", "应收合计", Decimal(totals_info["total"]), amount_parts, "元",
    )

    hit = len(invoice)
    miss = len(unbilled)
    version_parts = [{"label": "单选命中", "value": hit}]
    if miss:
        version_parts.append({"label": "无命中或重叠", "value": miss})
    version_account = _account(
        "version", "版本账", "本期运单", len(scope.waybills), version_parts, "行",
    )

    actual = sum(1 for line in invoice if line["weight_source"] in (billable.ACTUAL, billable.TIE))
    volumetric = sum(1 for line in invoice if line["weight_source"] == billable.VOLUMETRIC)
    weight_parts = [
        {"label": "实重择用", "value": actual},
        {"label": "体积重择用", "value": volumetric},
    ]
    weight_account = _account("weight", "计费重账", "计费行数", len(invoice), weight_parts, "行")
    return [rows_account, amount_account, version_account, weight_account]


def _account(key: str, title: str, left_label: str, left_value, parts: list[dict],
             unit: str) -> dict:
    zero = Decimal("0") if unit == "元" else 0
    right = zero
    for part in parts:
        right = right + part["value"]
    delta = left_value - right
    return {
        "key": key,
        "title": title,
        "left_label": left_label,
        "left_value": left_value,
        "parts": parts,
        "right_value": right,
        "delta": delta,
        "balanced": delta == 0,
        "unit": unit,
    }


def manifest_doc(period: str, version_id: str, version_no: int, basis: str,
                 rate_digest: str, scope: scope_mod.Scope, invoice: list[dict],
                 unbilled: list[dict], totals_info: dict, verdict: dict,
                 created_at: str) -> dict:
    """可复算清单：输入来源、指纹、参数、行数与合计。不含“当前时间”，重算稳定。"""
    return {
        "product": "freightrate",
        "period": period,
        "version_id": version_id,
        "version_no": version_no,
        "state_at_build": "draft",
        "created_at": created_at,
        "rules_version": config.RULES_VERSION,
        "profile": config.PROFILE,
        "basis_digest": basis,
        "rate_digest": rate_digest,
        "rate_set_version": max(
            (int(card["rate_set_version"]) for card in scope.cards), default=0
        ),
        "display_tz": config.DISPLAY_TZ,
        "inputs": [
            {
                "kind": source["kind"],
                "source_id": source["source_id"],
                "run_id": source["run_id"],
                "input_name": source["input_name"],
                "input_sha256": source["input_sha256"],
                "added_at": source["added_at"],
            }
            for source in scope.sources
        ],
        "rows": {
            "waybills": len(scope.waybills),
            "billed": len(invoice),
            "unbilled": len(unbilled),
            "superseded": len(scope.superseded),
            "excluded": len(scope.excluded),
            "quarantined": len(scope.quarantine),
        },
        "amount": {
            "parts": totals_info["parts"],
            "total": totals_info["total"],
            "conserved": totals_info["conserved"],
        },
        "reconcile": verdict,
        "params": {"profile": config.PROFILE, "quant": "0.01", "step": "0.5",
                   "period_tz": config.DISPLAY_TZ},
    }


def verdict_of(accounts: list[dict]) -> dict:
    broken = [account["key"] for account in accounts if not account["balanced"]]
    return {"balanced": not broken, "broken": broken, "count": len(accounts)}


def serialize_accounts(accounts: list[dict]) -> list[dict]:
    """把账本里的 Decimal 转成字符串，清单 JSON 才能稳定落库与复算。"""
    return [
        {
            **account,
            "left_value": str(account["left_value"]),
            "right_value": str(account["right_value"]),
            "delta": str(account["delta"]),
            "parts": [
                {**part, "value": str(part["value"])} for part in account["parts"]
            ],
        }
        for account in accounts
    ]
