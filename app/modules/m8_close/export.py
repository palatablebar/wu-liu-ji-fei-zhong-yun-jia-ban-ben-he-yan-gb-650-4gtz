"""正式版导出：账单、证据、影响、清单。

全部从版本快照生成，纯函数，不读运行期预览数据，因此同一个 version_id 的导出
字节级稳定；导出与发布共用同一份快照，不存在“导出时又变了”的窗口。
"""

from __future__ import annotations

import csv
import io
import json

INVOICE_HEADER = [
    "period", "version_no", "waybill_no", "carrier", "service", "zone", "pickup_utc",
    "billable_kg", "steps", "rate_version_id", "rate_set_version", "effective_from",
    "effective_to", "first_price", "step_price", "min_charge", "fuel_pct", "remote_fee",
    "insurance_pct", "base_amount", "step_amount", "remote_amount", "fuel_amount",
    "insurance_amount", "total_amount", "currency", "source_input_name", "source_line_no",
    "reason_codes",
]

EVIDENCE_HEADER = [
    "row_kind", "waybill_no", "carrier", "service", "zone", "pickup_utc", "actual_kg",
    "volumetric_kg", "weight_source", "billable_kg", "steps", "rate_version_id",
    "base_amount", "step_amount", "remote_amount", "fuel_amount", "insurance_amount",
    "total_amount", "source_input_name", "source_line_no", "reason_codes",
    "reason_labels", "detail",
]

IMPACT_HEADER = [
    "waybill_no", "change", "rate_changed", "weight_changed", "amount_changed",
    "old_version_id", "new_version_id", "old_billable_kg", "new_billable_kg",
    "old_total", "new_total", "delta_total", "delta_billable_kg", "changed_parts",
]


def _join(codes) -> str:
    if isinstance(codes, str):
        return " ".join(part for part in codes.replace(",", " ").split() if part)
    return " ".join(str(code) for code in codes)


def invoice_csv(invoice: list[dict], version: dict | None = None) -> str:
    version = version or {}
    period = version.get("period", "")
    version_no = version.get("version_no", "")
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(INVOICE_HEADER)
    for line in invoice:
        writer.writerow([
            period, version_no, line["waybill_no"], line["carrier"], line["service"],
            line["zone"], line["pickup_utc"], line["billable_kg"], line["steps"],
            line["rate_version_id"], line["rate_set_version"], line["effective_from"],
            line["effective_to"], line["first_price"], line["step_price"],
            line["min_charge"], line["fuel_pct"], line["remote_fee"],
            line["insurance_pct"], line["base_amount"], line["step_amount"],
            line["remote_amount"], line["fuel_amount"], line["insurance_amount"],
            line["total_amount"], line["currency"], line["source_input_name"],
            line["source_line_no"], _join(line.get("codes", "")),
        ])
    return buffer.getvalue()


def evidence_csv(evidence: list[dict]) -> str:
    from app.reasons import label

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EVIDENCE_HEADER)
    for row in evidence:
        doc = json.loads(row["payload"] or "{}")
        codes = _join(doc.get("codes", ""))
        detail = doc.get("detail", "")
        if not detail and doc.get("kind") == "excluded":
            detail = doc.get("reason", "")
        if not detail and doc.get("kind") == "superseded":
            detail = doc.get("changed_fields", "")
        writer.writerow([
            row["row_kind"], row["waybill_no"], doc.get("carrier", ""),
            doc.get("service", ""), doc.get("zone", ""), doc.get("pickup_utc", ""),
            doc.get("actual_kg", ""), doc.get("volumetric_kg", ""),
            doc.get("weight_source", doc.get("source", "")), doc.get("billable_kg", ""),
            doc.get("steps", ""), doc.get("rate_version_id", doc.get("version_id", "")),
            doc.get("base_amount", ""), doc.get("step_amount", ""),
            doc.get("remote_amount", ""), doc.get("fuel_amount", ""),
            doc.get("insurance_amount", ""), doc.get("total_amount", ""),
            doc.get("source_input_name", doc.get("input_name", "")),
            doc.get("source_line_no", doc.get("line_no", "")),
            codes, " ".join(label(code) for code in codes.split()), detail,
        ])
    return buffer.getvalue()


def impact_csv(impacts: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(IMPACT_HEADER)
    for row in impacts:
        writer.writerow([
            row["waybill_no"], row["change"], row["rate_changed"], row["weight_changed"],
            row["amount_changed"], row["old_version_id"], row["new_version_id"],
            row["old_billable_kg"], row["new_billable_kg"], row["old_total"],
            row["new_total"], row["delta_total"], row["delta_billable_kg"],
            row["changed_parts"],
        ])
    return buffer.getvalue()


def manifest_json(manifest_row: dict) -> str:
    doc = manifest_row.get("doc") or "{}"
    try:
        parsed = json.loads(doc)
    except ValueError:
        parsed = {}
    return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True, default=str)
