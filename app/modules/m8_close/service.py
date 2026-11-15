"""M8 编排：月结关账。

流程固定为四步，每一步都可重复执行且结果稳定：

1. ``add_rate_source``：把补发的运价文件登记到本期。校验不过不登记。
2. ``add_waybill_source``：把补发的运单文件采信并登记到本期。同一文件只登记一次。
3. ``build_version``：按当前范围算一份新版本（草稿 -> 已发布）。相同范围返回同一个
   版本，不新增；因此补发反复重试既不会新增版本，也不会重复生成账单。
4. ``publish``：复核通过后原子切换正式指针；旧版本整份保留为 superseded。

账单一经写入版本快照就不再变，正式账单、证据与导出全部从快照读。
"""

from __future__ import annotations

import hashlib

from app import config, manifest, store
from app.errors import DomainError
from app.modules.m1_waybill import service as waybill_service
from app.modules.m2_rates import ratecard, service as rate_service
from app.modules.m8_close import billing, scope as scope_mod

STATE_DRAFT = store.CLOSE_DRAFT
STATE_PUBLISHED = store.CLOSE_PUBLISHED
STATE_SUPERSEDED = store.CLOSE_SUPERSEDED


def ensure_period(period: str, note: str = "") -> dict:
    if not scope_mod.valid_period(period):
        raise DomainError("PERIOD_INVALID", f"关账期格式应为 YYYY-MM：{period}", 422)
    now = manifest.utc_now_iso()
    store.upsert_close_period(period, note, now)
    return store.close_period(period) or {}


def current_period() -> str:
    """默认关账期：最后打开的期；没有时按当前时间取月。"""
    rows = store.rows("SELECT period FROM close_periods ORDER BY updated_at DESC, period DESC LIMIT 1")
    if rows:
        return rows[0]["period"]
    now = manifest.utc_now_iso()
    return scope_mod.period_of(now)


def seed_sample() -> dict:
    """首次启动给页面一个可直接演示的关账期：登记样例运价与运单并生成首版。

    只在库里还没有任何关账期时动作；之后无论重启多少次都不会新增来源或版本。
    样例运单的提货时刻落在 2026-04，所以期固定取这个月。
    """
    if store.rows("SELECT period FROM close_periods LIMIT 1"):
        return {"applied": False, "period": current_period()}
    period = "2026-04"
    try:
        add_rate_source(period, (config.SAMPLE_DIR / config.SAMPLE_RATES).read_bytes(),
                        config.SAMPLE_RATES)
        add_waybill_source(period, (config.SAMPLE_DIR / config.SAMPLE_WAYBILLS).read_bytes(),
                           config.SAMPLE_WAYBILLS)
        built = build_version(period, "样例首版")
    except (OSError, DomainError):
        return {"applied": False, "period": period}
    return {"applied": built["changed"], "period": period,
            "version_id": built["version"].get("version_id", "")}


# --- 来源登记 ---------------------------------------------------------------

def _rate_source_id(digest: str) -> str:
    return "rate:" + digest[:16]


def _current_rate_set_version(period: str) -> int:
    """本期已采用运价里最高的运价集版本（按承运商合并，不是单看最后一份文件）。"""
    versions = []
    for source in store.close_sources(period, "rate"):
        for card in store.close_rate_cards(period, source["source_id"]):
            versions.append(int(card["rate_set_version"]))
    return max(versions, default=0)


def add_rate_source(period: str, raw: bytes, name: str) -> dict:
    """登记一份补发运价。重叠、陈旧、缺列一律拒绝，现有范围保持原样。"""
    ensure_period(period)
    report = rate_service.audit(raw, name)
    if report["problems"]:
        raise DomainError("RATE_PARSE_FAILED", ";".join(report["problems"][:5]), 422)
    if report["overlaps"]:
        first = report["overlaps"][0]
        raise DomainError(
            "RATE_OVERLAP",
            f"{first['carrier']}/{first['service']}/{first['zone']} "
            f"{first['left']} 与 {first['right']} 生效区间重叠",
            409,
        )
    set_version = int(report["rate_set_version"])
    current = _current_rate_set_version(period)
    if current and set_version < current:
        raise DomainError(
            "RATE_STALE",
            f"补发运价集版本 {set_version} 低于本期已采用 {current}",
            409,
        )
    text, _codes = ratecard.read_text(raw)
    cards, _problems, _version = ratecard.parse_cards(text)
    digest = hashlib.sha256(raw).hexdigest()
    source_id = _rate_source_id(digest)
    existing = [s for s in store.close_sources(period, "rate") if s["source_id"] == source_id]
    if existing:
        return {"added": False, "source": existing[0], "rate_set_version": set_version,
                "count": len(cards), "digest": digest}
    source = {
        "source_id": source_id,
        "input_name": name,
        "input_sha256": digest,
        "detail": f"{set_version}:{report['count']}",
        "added_at": manifest.utc_now_iso(),
    }
    added = store.write_rate_source(period, source, cards)
    return {"added": added, "source": source, "rate_set_version": set_version,
            "count": len(cards), "digest": digest}


def add_waybill_source(period: str, raw: bytes, name: str) -> dict:
    """采信并登记一份补发运单。内容相同的文件重复提交不新增来源。

    关账采信只做单行解析，不按当前运价表判断引用：本期用哪份运价由关账范围决定，
    而且补发运价与补发运单的到达顺序不可控。运价缺哪家只让对应票变成“未计费”，
    不会把整行从采信表里删掉，也不会因为先收运单后收运价而误隔离。
    """
    ensure_period(period)
    ingest = waybill_service.import_bytes(raw, name, check_references=False)
    digest = ingest["input_sha256"]
    source_id = "waybill:" + digest[:16]
    source = {
        "source_id": source_id,
        "run_id": ingest["run_id"],
        "input_name": name,
        "input_sha256": digest,
        "detail": f"{ingest['rows_ok']}ok/{ingest['rows_quarantined']}q",
        "added_at": manifest.utc_now_iso(),
    }
    added = store.write_waybill_source(period, source)
    return {
        "added": added,
        "source": source,
        "run_id": ingest["run_id"],
        "rows_in": ingest["rows_in"],
        "rows_ok": ingest["rows_ok"],
        "rows_quarantined": ingest["rows_quarantined"],
        "reused_run": ingest.get("reused", False),
    }


# --- 构建版本 ---------------------------------------------------------------

def baseline_version(period: str) -> dict | None:
    """基线固定为“最近一次正式发布”，不受草稿影响。"""
    official = store.official_version_id(period)
    if official:
        version = store.close_version(official)
        if version:
            return version
    return None


def _snapshot_scope(version_id: str) -> dict:
    return {
        "quarantined": [
            {
                "run_id": row["run_id"],
                "line_no": row["line_no"],
                "code": row["code"],
                "waybill_no": row["waybill_no"],
            }
            for row in store.close_scope_rows(version_id, "quarantined")
        ],
        "excluded": [
            {
                "run_id": row["run_id"],
                "line_no": row["line_no"],
                "waybill_no": row["waybill_no"],
            }
            for row in store.close_scope_rows(version_id, "excluded")
        ],
        "superseded": [
            {
                "waybill_no": row["waybill_no"],
                "from_run_id": "",
                "to_run_id": row["run_id"],
                "changed_fields": row["detail"],
            }
            for row in store.close_scope_rows(version_id, "superseded")
        ],
    }


def _baseline_invoice(version: dict | None) -> list[dict]:
    if not version:
        return []
    return [
        {
            "waybill_no": row["waybill_no"],
            "billable_kg": row["billable_kg"],
            "rate_version_id": row["rate_version_id"],
            "rate_set_version": row["rate_set_version"],
            "total_amount": row["total_amount"],
            "base_amount": row["base_amount"],
            "step_amount": row["step_amount"],
            "remote_amount": row["remote_amount"],
            "fuel_amount": row["fuel_amount"],
            "insurance_amount": row["insurance_amount"],
        }
        for row in store.close_invoice_lines(version["version_id"])
    ]


def build_version(period: str, note: str = "") -> dict:
    """按当前范围构建版本。范围指纹相同则返回已有版本，不新增。"""
    ensure_period(period, note)
    scope = scope_mod.resolve(period)
    if not scope.rate_source:
        raise DomainError("CLOSE_NO_RATES", "本期还没有登记运价，先补发或装载运价", 409)
    if not any(source["kind"] == "waybill" for source in scope.sources):
        raise DomainError("CLOSE_NO_SOURCES", "本期还没有登记运单", 409)

    basis = scope_mod.basis_digest(scope)
    existing = store.close_version_by_basis(period, basis)
    if existing:
        return {
            "version": existing,
            "reused": True,
            "scope": scope.summary(),
            "changed": False,
        }

    baseline = baseline_version(period)
    baseline_invoice = _baseline_invoice(baseline)
    invoice, unbilled = billing.price_scope(scope)
    totals_info = billing.invoice_totals(invoice)
    impacts = billing.compute_impacts(
        invoice, baseline_invoice, scope, _snapshot_scope(baseline["version_id"]) if baseline else None
    )
    accounts = billing.build_accounts(scope, invoice, unbilled, totals_info)
    verdict = billing.verdict_of(accounts)
    if not totals_info["conserved"]:
        raise DomainError("CLOSE_NOT_CONSERVED", "分项之和与应收不等，拒绝生成版本", 409)

    impact_digest = billing.impact_digest(impacts, scope)
    rate_digest = scope_mod.rate_digest(scope.cards)
    created_at = manifest.utc_now_iso()
    scope_rows = billing.build_scope_rows(invoice, unbilled, scope)
    evidence = billing.build_evidence(invoice, unbilled, scope)

    # 版次号在写锁内取：并发构建时另一方可能先占号，唯一索引会拒绝，重取即可。
    for attempt in range(3):
        version_no = store.next_close_version_no(period)
        version_id = f"CLOSE-{period}-v{version_no}"
        doc = billing.manifest_doc(
            period, version_id, version_no, basis, rate_digest, scope,
            invoice, unbilled, totals_info, verdict, created_at,
        )
        doc["impact"] = {
            "digest": impact_digest,
            "added": impacts["added"],
            "removed": impacts["removed"],
            "changed": impacts["changed"],
            "unchanged": impacts["unchanged"],
            "delta_total": impacts["delta_total"],
            "delta_billable_kg": impacts["delta_billable_kg"],
            "baseline_version_id": baseline["version_id"] if baseline else "",
        }
        doc["accounts"] = billing.serialize_accounts(accounts)

        snapshot = {
            "version": {
                "version_id": version_id,
                "period": period,
                "version_no": version_no,
                "state": STATE_DRAFT,
                "basis_digest": basis,
                "impact_digest": impact_digest,
                "rate_digest": rate_digest,
                "rate_set_version": doc["rate_set_version"],
                "rules_version": config.RULES_VERSION,
                "baseline_version_id": baseline["version_id"] if baseline else "",
                "source_count": len(scope.sources),
                "scope_count": len(scope.waybills),
                "billed_count": len(invoice),
                "unbilled_count": len(unbilled),
                "excluded_count": len(scope.excluded),
                "superseded_count": len(scope.superseded),
                "quarantined_count": len({(q["run_id"], q["line_no"]) for q in scope.quarantine}),
                "amount_total": totals_info["total"],
                "balanced": verdict["balanced"],
                "created_at": created_at,
                "published_at": "",
                "superseded_at": "",
            },
            "invoice": invoice,
            "evidence": evidence,
            "scope": scope_rows,
            "impacts": impacts["entries"],
            "manifest": manifest.dumps(doc),
        }
        try:
            store.write_close_version(snapshot)
        except Exception:
            # 并发构建同一范围时唯一索引会拒绝第二个；此时已有版本就是答案，不重复出账。
            existing = store.close_version_by_basis(period, basis)
            if existing:
                return {"version": existing, "reused": True, "changed": False,
                        "scope": scope.summary()}
            if attempt == 2:
                raise
            continue
        return {
            "version": store.close_version(version_id) or {},
            "reused": False,
            "changed": True,
            "scope": scope.summary(),
            "manifest": doc,
        }
    raise DomainError("CLOSE_BUILD_CONFLICT", "并发构建冲突，请重试", 409)


# --- 复核与发布 -------------------------------------------------------------

def impact_report(version_id: str) -> dict:
    """补发影响：只从版本快照读，保证复核人看到的与将要发布的完全一致。"""
    version = _require(version_id)
    entries = store.close_impacts(version_id)
    quarantine = store.close_scope_rows(version_id, "quarantined")
    excluded = store.close_scope_rows(version_id, "excluded")
    superseded = store.close_scope_rows(version_id, "superseded")
    unchanged = max(int(version.get("scope_count") or 0) - len(entries), 0)
    return {
        "version": version,
        "baseline_version_id": version.get("baseline_version_id", ""),
        "entries": entries,
        "groups": _group_impacts(entries),
        "new_quarantine": quarantine,
        "new_excluded": excluded,
        "new_superseded": superseded,
        "unchanged": unchanged,
        "totals": {
            "delta_total": _sum_decimal(entries, "delta_total", "0.00"),
            "delta_billable_kg": _sum_decimal(entries, "delta_billable_kg", "0.000"),
            "old_total": _sum_decimal(entries, "old_total", "0.00"),
            "new_total": _sum_decimal(entries, "new_total", "0.00"),
        },
        "impact_digest": version.get("impact_digest", ""),
        "reviews": store.close_reviews(version["period"], version_id),
    }


def _sum_decimal(items: list[dict], field: str, default: str) -> str:
    from decimal import Decimal

    return str(sum((Decimal(str(item.get(field) or default)) for item in items),
                   Decimal(default)))


def _group_impacts(entries: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for entry in entries:
        groups.setdefault(entry["change"], []).append(entry)
    labels = {"added": "新增计费", "changed": "计费改变", "removed": "移出计费"}
    return [
        {"change": key, "label": labels.get(key, key), "count": len(groups[key]),
         "waybills": groups[key]}
        for key in ("changed", "added", "removed") if key in groups
    ]


def record_review(version_id: str, reviewer: str, note: str = "") -> dict:
    version = _require(version_id)
    if version["state"] != STATE_DRAFT:
        raise DomainError("CLOSE_ALREADY_PUBLISHED", "该版本已发布，复核结论不可追加", 409)
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise DomainError("CLOSE_REVIEWER_REQUIRED", "复核人不能为空", 422)
    store.add_close_review(
        version["period"], version_id, reviewer, note.strip(),
        version.get("impact_digest", ""), manifest.utc_now_iso(),
    )
    return {"version_id": version_id, "reviewer": reviewer,
            "reviews": store.close_reviews(version["period"], version_id)}


def publish(version_id: str, reviewer: str = "", note: str = "") -> dict:
    """复核通过后发布：要求存在复核人，且复核绑定的影响指纹与当前版本一致。"""
    version = _require(version_id)
    if version["state"] == STATE_PUBLISHED and store.official_version_id(version["period"]) == version_id:
        return {"published": False, "already": True, "version": version,
                "pointer": version_id}
    if version["state"] == STATE_SUPERSEDED:
        raise DomainError("CLOSE_SUPERSEDED", "该版本已被后续版本取代，不能再发布", 409)

    reviews = store.close_reviews(version["period"], version_id)
    if reviewer:
        store.add_close_review(
            version["period"], version_id, reviewer.strip(), note.strip(),
            version.get("impact_digest", ""), manifest.utc_now_iso(),
        )
        reviews = store.close_reviews(version["period"], version_id)
    if not reviews:
        raise DomainError("CLOSE_REVIEW_REQUIRED", "发布前必须有复核记录", 409)
    stale = [r for r in reviews if r["impact_digest"] != version.get("impact_digest", "")]
    if stale and len(stale) == len(reviews):
        raise DomainError("CLOSE_REVIEW_STALE", "复核结论绑定的影响已变化，需重新复核", 409)

    result = store.publish_close_version(version["period"], version_id, manifest.utc_now_iso())
    return {
        "published": result["changed"],
        "already": not result["changed"],
        "previous_version_id": result["previous"],
        "pointer": result["pointer"],
        "version": store.close_version(version_id) or {},
    }


def version_or_none(version_id: str) -> dict | None:
    return store.close_version(version_id) if version_id else None


def _require(version_id: str) -> dict:
    version = store.close_version(version_id)
    if not version:
        raise DomainError("CLOSE_VERSION_NOT_FOUND", f"版本不存在：{version_id}", 404)
    return version


# --- 查询与导出 -------------------------------------------------------------

def official_version(period: str) -> dict | None:
    version_id = store.official_version_id(period)
    if not version_id:
        return None
    return store.close_version(version_id)


def effective_version(period: str) -> dict | None:
    """正式指针优先；还没发布时回落到最新草稿，供复核人预览。"""
    official = official_version(period)
    if official:
        return official
    versions = store.close_versions(period)
    return versions[-1] if versions else None


def review_target(period: str) -> dict | None:
    """复核人该看的版本：有草稿就看待复核的最新草稿，否则看正式版。"""
    drafts = [v for v in store.close_versions(period) if v["state"] == STATE_DRAFT]
    if drafts:
        return drafts[-1]
    return official_version(period)


def period_summary(period: str) -> dict:
    scope = scope_mod.resolve(period)
    official = official_version(period)
    versions = store.close_versions(period)
    return {
        "period": period,
        "row": store.close_period(period) or {},
        "scope": scope.summary(),
        "official": official,
        "official_version_id": official["version_id"] if official else "",
        "versions": [
            {
                **version,
                "is_official": bool(official and version["version_id"] == official["version_id"]),
            }
            for version in versions
        ],
        "pending_review": [
            version for version in versions
            if version["state"] == STATE_DRAFT
        ],
    }


def version_detail(version_id: str) -> dict:
    version = _require(version_id)
    manifest_row = store.close_manifest(version_id) or {}
    doc = _loads(manifest_row.get("doc", "{}"))
    invoice = store.close_invoice_lines(version_id)
    evidence = store.close_evidence_rows(version_id)
    return {
        "version": version,
        "manifest": doc,
        "accounts": _loads(doc.get("accounts") or "[]"),
        "invoice": invoice,
        "evidence": evidence,
        "impacts": store.close_impacts(version_id),
        "scope_rows": store.close_scope_rows(version_id),
        "reviews": store.close_reviews(version["period"], version_id),
    }


def version_accounts(version_id: str) -> list[dict]:
    manifest_row = store.close_manifest(version_id) or {}
    doc = _loads(manifest_row.get("doc", "{}")) or {}
    return _loads(doc.get("accounts") or "[]") or []


def _loads(text):
    import json

    if not isinstance(text, (str, bytes, bytearray)):
        return text
    try:
        return json.loads(text or "null")
    except ValueError:
        return None


def invoice_csv(version_id: str) -> str:
    from app.modules.m8_close import export as export_mod

    version = _require(version_id)
    return export_mod.invoice_csv(store.close_invoice_lines(version_id), version)


def evidence_csv(version_id: str) -> str:
    from app.modules.m8_close import export as export_mod

    return export_mod.evidence_csv(store.close_evidence_rows(version_id))


def impact_csv(version_id: str) -> str:
    from app.modules.m8_close import export as export_mod

    return export_mod.impact_csv(store.close_impacts(version_id))


def manifest_json(version_id: str) -> str:
    from app.modules.m8_close import export as export_mod

    return export_mod.manifest_json(store.close_manifest(version_id) or {})
