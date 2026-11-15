"""M8 月结出账：范围确定、补发影响、复核、原子切换、幂等与中断恢复。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from fixtures import close_samples
from fixtures.expected import AMOUNT_TOTAL

from app import config, store
from app.errors import DomainError
from app.modules.m8_close import service as close

PERIOD = "2026-04"

_CLOSE_TABLES = (
    "close_periods", "close_sources", "close_versions", "close_invoice_lines",
    "close_evidence_rows", "close_scope_rows", "close_impacts", "close_manifests",
    "close_reviews", "close_official", "close_rate_cards",
)


@pytest.fixture()
def period():
    """每个用例都从空关账期开始，互不影响；期固定为样例运单所在的 2026-04。"""
    with store.session() as conn:
        for table in _CLOSE_TABLES:
            conn.execute(f"DELETE FROM {table}")
    return PERIOD


def _clean_rates() -> bytes:
    return (config.SAMPLE_DIR / config.SAMPLE_RATES).read_bytes()


def _dirty() -> bytes:
    return (config.SAMPLE_DIR / config.SAMPLE_WAYBILLS).read_bytes()


def _seed(period: str, waybills: bytes = b"", rate: bytes = b"", name_suffix: str = ""):
    close.ensure_period(period)
    close.add_rate_source(period, rate or _clean_rates(), f"rate{name_suffix}.csv")
    if waybills is not None:
        close.add_waybill_source(
            period, waybills if waybills else _dirty(), f"waybills{name_suffix}.csv",
        )
    return close.build_version(period)


def _publish(result: dict, reviewer: str = "复核人甲") -> dict:
    version_id = result["version"]["version_id"]
    close.record_review(version_id, reviewer, "初审通过")
    return close.publish(version_id)


# --- 范围：本次出账用哪版运价、哪批运单 -------------------------------------

def test_first_version_bills_only_in_period_waybills(period):
    result = _seed(period)
    version = result["version"]
    # 脏运单 27 行：10 行真隔离（脏值/截断/冲突），2 行运价覆盖不到进未计费，
    # 1 行是 3 月票进超范围，其余 14 票计费。金额与预览流水一致。
    assert version["billed_count"] == 14
    assert version["unbilled_count"] == 2
    assert version["excluded_count"] >= 1
    assert version["quarantined_count"] == 10
    assert version["amount_total"] == "1501.60"
    assert version["balanced"] == 1


def test_scope_records_source_files_and_digests(period):
    result = _seed(period)
    scope = result["scope"]
    kinds = [source["kind"] for source in scope["sources"]]
    assert kinds == ["rate", "waybill"]
    detail = close.version_detail(result["version"]["version_id"])
    inputs = detail["manifest"]["inputs"]
    assert {item["kind"] for item in inputs} == {"rate", "waybill"}
    assert all(len(item["input_sha256"]) == 64 for item in inputs)


def test_wrong_period_is_excluded_not_billed(period):
    result = _seed(period)
    detail = close.version_detail(result["version"]["version_id"])
    excluded = [row for row in detail["scope_rows"] if row["kind"] == "excluded"]
    assert [row["waybill_no"] for row in excluded] == ["WB-1011"]
    assert excluded[0]["code"] == "PERIOD_OUT_OF_SCOPE"
    billed = {row["waybill_no"] for row in detail["invoice"]}
    assert "WB-1011" not in billed


# --- 补发：历史修正 + 迟到运单 ----------------------------------------------

def test_resend_supersedes_history_and_adds_late_waybill(period):
    first = _seed(period)
    _publish(first)
    before = {row["waybill_no"]: row for row in store.close_invoice_lines(
        first["version"]["version_id"])}
    assert before["WB-1001"]["billable_kg"] == "5.000"
    assert before["WB-1001"]["source_input_name"] == "waybills.csv"

    added = close.add_waybill_source(
        period, close_samples.resend_bytes(), "waybills_resend.csv",
    )
    assert added["added"] is True

    second = close.build_version(period)
    assert second["version"]["version_no"] == 2
    after = {row["waybill_no"]: row for row in store.close_invoice_lines(
        second["version"]["version_id"])}
    # 历史修正：同号取补发值，来源行指向补发文件
    assert after["WB-1001"]["source_input_name"] == "waybills_resend.csv"
    assert after["WB-1001"]["billable_kg"] == "6.500"
    assert after["WB-1001"]["rate_version_id"] == "SF-standard-Z1-v2"
    # 迟到运单进入本期
    assert "WB-1030" in after
    # 超范围行不进入账单
    assert "WB-1031" not in after


def test_resend_changes_are_itemised_for_reviewer(period):
    first = _seed(period)
    _publish(first)
    close.add_waybill_source(period, close_samples.resend_bytes(), "waybills_resend.csv")
    second = close.build_version(period)
    impact = close.impact_report(second["version"]["version_id"])

    groups = {group["change"]: group for group in impact["groups"]}
    assert [row["waybill_no"] for row in groups["added"]["waybills"]] == ["WB-1030"]
    # 历史修正改了重量：计费重与应收都变，必须逐票列在"计费改变"里
    changed = {row["waybill_no"]: row for row in groups["changed"]["waybills"]}
    assert "WB-1001" in changed
    entry = changed["WB-1001"]
    assert entry["weight_changed"] == 1
    assert (entry["old_billable_kg"], entry["new_billable_kg"]) == ("5.000", "6.500")
    assert Decimal(entry["delta_total"]) > 0
    assert entry["delta_total"] == str(
        Decimal(entry["new_total"]) - Decimal(entry["old_total"])
    )
    # 未变的票不列进影响；unchanged 覆盖本期全部运单减掉列出的差异票
    assert impact["unchanged"] == second["version"]["scope_count"] - len(impact["entries"])
    assert all(row["waybill_no"] != "WB-1002" for row in impact["entries"])


def test_impact_lists_superseded_and_excluded_events(period):
    first = _seed(period)
    _publish(first)
    close.add_waybill_source(period, close_samples.resend_bytes(), "waybills_resend.csv")
    second = close.build_version(period)
    impact = close.impact_report(second["version"]["version_id"])
    superseded = [(row["waybill_no"], row["detail"]) for row in impact["new_superseded"]]
    assert ("WB-1001", "weight_kg") in superseded
    excluded = [row["waybill_no"] for row in impact["new_excluded"]]
    assert "WB-1031" in excluded


def test_rate_resend_changes_version_and_amount(period):
    first = _seed(period)
    _publish(first)
    # 运价集版本更高的补发价整体上调 SF/Z1 4 月档
    close.add_rate_source(period, close_samples.rate_resend_bytes(_clean_rates()), "rates_v4.csv")
    second = close.build_version(period)
    assert second["version"]["rate_set_version"] == 4
    impact = close.impact_report(second["version"]["version_id"])
    changed = {row["waybill_no"]: row for row in impact["entries"]}
    sf_rows = [
        row for row in store.close_invoice_lines(second["version"]["version_id"])
        if row["carrier"] == "SF" and row["zone"] == "Z1"
    ]
    assert sf_rows
    assert {row["rate_set_version"] for row in sf_rows} == {4}
    assert any(row["rate_changed"] for row in changed.values())
    assert Decimal(impact["totals"]["delta_total"]) != 0


def test_stale_rate_resend_is_rejected(period):
    _seed(period)
    stale = close_samples.rate_resend_bytes(_clean_rates(), set_version=1)
    with pytest.raises(DomainError) as excinfo:
        close.add_rate_source(period, stale, "rates_stale.csv")
    assert excinfo.value.code == "RATE_STALE"
    # 拒绝登记后范围不变
    assert len(store.close_sources(period, "rate")) == 1


def test_overlapping_rate_resend_is_rejected(period):
    _seed(period)
    overlap = (config.SAMPLE_DIR / config.SAMPLE_RATES_OVERLAP).read_bytes()
    with pytest.raises(DomainError) as excinfo:
        close.add_rate_source(period, overlap, "rates_overlap.csv")
    assert excinfo.value.code == "RATE_OVERLAP"
    assert len(store.close_sources(period, "rate")) == 1


# --- 幂等：补发反复重试不新增版本、不重复出账 --------------------------------

def test_repeated_resend_does_not_add_source_or_version(period):
    first = _seed(period)
    _publish(first)
    first_add = close.add_waybill_source(
        period, close_samples.resend_bytes(), "waybills_resend.csv",
    )
    assert first_add["added"] is True
    second = close.build_version(period)
    assert second["reused"] is False
    for _ in range(3):
        added = close.add_waybill_source(
            period, close_samples.resend_bytes(), "waybills_resend.csv",
        )
        assert added["added"] is False
        result = close.build_version(period)
        assert result["reused"] is True
        assert result["version"]["version_id"] == second["version"]["version_id"]
    versions = store.close_versions(period)
    assert [v["version_no"] for v in versions] == [1, 2]
    assert len(store.close_sources(period, "waybill")) == 2


def test_rebuild_same_scope_returns_same_version(period):
    first = _seed(period)
    again = close.build_version(period)
    assert again["reused"] is True
    assert again["version"]["version_id"] == first["version"]["version_id"]
    assert len(store.close_invoice_lines(first["version"]["version_id"])) == 14


def test_resend_after_resend_creates_one_more_version_at_most(period):
    first = _seed(period)
    _publish(first)
    close.add_waybill_source(period, close_samples.resend_bytes(), "r1.csv")
    second = close.build_version(period)
    assert second["version"]["version_no"] == 2
    # 再提交同一份补发，范围指纹不变
    close.add_waybill_source(period, close_samples.resend_bytes(), "r1.csv")
    third = close.build_version(period)
    assert third["version"]["version_id"] == second["version"]["version_id"]
    assert len(store.close_versions(period)) == 2


# --- 复核与原子切换 ---------------------------------------------------------

def test_publish_requires_review(period):
    result = _seed(period)
    with pytest.raises(DomainError) as excinfo:
        close.publish(result["version"]["version_id"])
    assert excinfo.value.code == "CLOSE_REVIEW_REQUIRED"


def test_review_requires_reviewer_name(period):
    result = _seed(period)
    with pytest.raises(DomainError) as excinfo:
        close.record_review(result["version"]["version_id"], "   ")
    assert excinfo.value.code == "CLOSE_REVIEWER_REQUIRED"


def test_stale_review_is_rejected(period):
    result = _seed(period)
    version_id = result["version"]["version_id"]
    close.record_review(version_id, "复核人甲", "看过")
    # 复核记录之后影响发生变化（改库模拟），旧复核不再有效
    store.update_close_version(version_id, {"impact_digest": "changed-after-review"})
    with pytest.raises(DomainError) as excinfo:
        close.publish(version_id)
    assert excinfo.value.code == "CLOSE_REVIEW_STALE"


def test_publish_is_atomic_pointer_switch(period):
    first = _seed(period)
    _publish(first)
    assert close.official_version(period)["version_id"] == first["version"]["version_id"]

    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")
    second = close.build_version(period)
    previous_id = close.official_version(period)["version_id"]
    close.record_review(second["version"]["version_id"], "复核人乙", "补发核过")
    outcome = close.publish(second["version"]["version_id"])

    assert outcome["published"] is True
    assert outcome["previous_version_id"] == previous_id
    assert close.official_version(period)["version_id"] == second["version"]["version_id"]
    assert store.close_version(previous_id)["state"] == store.CLOSE_SUPERSEDED
    # 旧版本整份保留，账单行数不变
    assert len(store.close_invoice_lines(previous_id)) == 14


def test_republish_official_version_is_noop(period):
    result = _seed(period)
    _publish(result)
    version_id = result["version"]["version_id"]
    again = close.publish(version_id)
    assert again["already"] is True
    assert again["published"] is False
    assert len(store.close_versions(period)) == 1


def test_superseded_version_cannot_be_republished(period):
    first = _seed(period)
    _publish(first)
    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")
    second = close.build_version(period)
    _publish(second)
    with pytest.raises(DomainError) as excinfo:
        close.publish(first["version"]["version_id"])
    assert excinfo.value.code == "CLOSE_SUPERSEDED"


# --- 正式账单/证据/导出一次性切换且不可变 -----------------------------------

def test_published_snapshot_is_immutable_after_later_resends(period):
    first = _seed(period)
    _publish(first)
    version_id = first["version"]["version_id"]
    invoice_before = close.invoice_csv(version_id)
    evidence_before = close.evidence_csv(version_id)
    manifest_before = close.manifest_json(version_id)

    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")
    close.build_version(period)
    close.add_rate_source(period, close_samples.rate_resend_bytes(_clean_rates()), "rv4.csv")
    close.build_version(period)

    assert close.invoice_csv(version_id) == invoice_before
    assert close.evidence_csv(version_id) == evidence_before
    assert close.manifest_json(version_id) == manifest_before


def test_official_exports_follow_the_pointer(period):
    first = _seed(period)
    _publish(first)
    official = close.official_version(period)
    assert close.invoice_csv(official["version_id"]) == close.invoice_csv(
        first["version"]["version_id"]
    )
    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")
    second = close.build_version(period)
    _publish(second)
    official = close.official_version(period)
    assert official["version_id"] == second["version"]["version_id"]
    assert len(store.close_invoice_lines(official["version_id"])) == 15


def test_evidence_covers_every_scope_outcome(period):
    result = _seed(period)
    detail = close.version_detail(result["version"]["version_id"])
    kinds = {row["row_kind"] for row in detail["evidence"]}
    assert kinds == {"invoice", "quarantine", "excluded", "unbilled"}
    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")
    second = close.build_version(period)
    detail = close.version_detail(second["version"]["version_id"])
    kinds = {row["row_kind"] for row in detail["evidence"]}
    assert kinds == {"invoice", "quarantine", "excluded", "unbilled", "superseded"}


def test_exports_are_deterministic(period):
    result = _seed(period)
    version_id = result["version"]["version_id"]
    assert close.invoice_csv(version_id) == close.invoice_csv(version_id)
    assert close.evidence_csv(version_id) == close.evidence_csv(version_id)
    assert close.manifest_json(version_id) == close.manifest_json(version_id)


# --- 中断：不产生半个版本、旧版原样 -----------------------------------------

def test_interrupted_write_leaves_no_partial_version(period, monkeypatch):
    first = _seed(period)
    _publish(first)
    baseline_official = close.official_version(period)["version_id"]
    baseline_invoice = close.invoice_csv(baseline_official)
    version_count = len(store.close_versions(period))

    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")

    calls = {"n": 0}
    original = store._insert_many

    def exploding(conn, table, cols, items):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("模拟写入中断")
        return original(conn, table, cols, items)

    monkeypatch.setattr(store, "_insert_many", exploding)
    with pytest.raises(RuntimeError):
        close.build_version(period)
    monkeypatch.setattr(store, "_insert_many", original)

    # 没有半成品版本，正式指针与旧账单都不变
    assert len(store.close_versions(period)) == version_count
    assert close.official_version(period)["version_id"] == baseline_official
    assert close.invoice_csv(close.official_version(period)["version_id"]) == baseline_invoice
    # 重试后正常得到新版本
    retried = close.build_version(period)
    assert retried["version"]["version_no"] == version_count + 1
    _publish(retried, "复核人戊")


def test_interrupted_publish_keeps_old_official(period, monkeypatch):
    first = _seed(period)
    _publish(first)
    baseline = close.official_version(period)["version_id"]

    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")
    second = close.build_version(period)
    close.record_review(second["version"]["version_id"], "复核人丙")

    original = store._point_official

    def boom(conn, period_arg, version_id, published_at):
        raise RuntimeError("切换中断")

    monkeypatch.setattr(store, "_point_official", boom)
    with pytest.raises(RuntimeError):
        close.publish(second["version"]["version_id"])
    monkeypatch.setattr(store, "_point_official", original)

    assert close.official_version(period)["version_id"] == baseline
    # 状态也回滚：新版本仍是草稿，旧版仍是正式
    assert store.close_version(second["version"]["version_id"])["state"] == store.CLOSE_DRAFT
    assert store.close_version(baseline)["state"] == store.CLOSE_PUBLISHED

    close.publish(second["version"]["version_id"])
    assert close.official_version(period)["version_id"] == second["version"]["version_id"]


# --- 账单自洽 ---------------------------------------------------------------

def test_four_accounts_balance_and_amounts_conserve(period):
    result = _seed(period)
    detail = close.version_detail(result["version"]["version_id"])
    accounts = {account["key"]: account for account in detail["accounts"]}
    assert len(accounts) == 4
    assert all(account["balanced"] is True for account in detail["accounts"])
    amount = accounts["amount"]
    assert amount["left_value"] == amount["right_value"] == "1501.60"
    assert detail["manifest"]["amount"]["conserved"] is True
    assert detail["manifest"]["reconcile"]["balanced"] is True


def test_billable_and_total_match_manual_calculation(period):
    result = _seed(period)
    invoice = {row["waybill_no"]: row for row in store.close_invoice_lines(
        result["version"]["version_id"])}
    assert invoice["WB-1001"]["base_amount"] == "13.00"
    assert invoice["WB-1001"]["step_amount"] == "20.80"
    assert invoice["WB-1001"]["total_amount"] == "42.67"
    assert invoice["WB-1002"]["total_amount"] == "20.62"
    assert invoice["WB-1005"]["total_amount"] == "697.05"


def test_close_total_matches_preview_minus_out_of_period(period):
    """月结与 M1-M5 预览用同一套规则；差额正好是 3 月那票（预览会算，本期不算）。"""
    result = _seed(period)
    detail = close.version_detail(result["version"]["version_id"])
    excluded_total = sum(
        (Decimal(row["total_amount"]) for row in store.close_invoice_lines(
            result["version"]["version_id"]) if False),
        Decimal("0.00"),
    )
    # 预览 1521.04 - WB-1011(19.44, 3 月票) = 本期 1501.60
    assert result["version"]["amount_total"] == str(
        Decimal(AMOUNT_TOTAL) - Decimal("19.44") + excluded_total
    )
    assert [row["waybill_no"] for row in detail["scope_rows"]
            if row["kind"] == "excluded"] == ["WB-1011"]


def test_unbilled_when_rate_missing(period):
    """运价里没有某家承运商时，其运单进未计费，而不是静默算 0。"""
    close.ensure_period(period)
    text = _clean_rates().decode("utf-8").splitlines()
    only_sf = "\n".join([line for line in text if line.startswith("carrier") or line.startswith("SF,")])
    close.add_rate_source(period, (only_sf + "\n").encode("utf-8"), "rate_sf_only.csv")
    close.add_waybill_source(period, _dirty(), "waybills.csv")
    result = close.build_version(period)
    assert result["version"]["unbilled_count"] > 0
    detail = close.version_detail(result["version"]["version_id"])
    assert any(row["kind"] == "unbilled" for row in detail["scope_rows"])
    assert detail["accounts"][2]["balanced"] is True


def test_period_validation(period):
    with pytest.raises(DomainError) as excinfo:
        close.ensure_period("2026-13")
    assert excinfo.value.code == "PERIOD_INVALID"
    with pytest.raises(DomainError):
        close.ensure_period("202604")


def test_build_requires_rate_and_waybills(period):
    close.ensure_period(period)
    with pytest.raises(DomainError) as excinfo:
        close.build_version(period)
    assert excinfo.value.code == "CLOSE_NO_RATES"
    close.add_rate_source(period, _clean_rates(), "rate.csv")
    with pytest.raises(DomainError) as excinfo:
        close.build_version(period)
    assert excinfo.value.code == "CLOSE_NO_SOURCES"


def test_impact_digest_tracks_scope_changes(period):
    first = _seed(period)
    digest_before = first["version"]["impact_digest"]
    close.add_waybill_source(period, close_samples.resend_bytes(), "r.csv")
    second = close.build_version(period)
    assert second["version"]["impact_digest"] != digest_before
    # 影响指纹与复核绑定：复核后即发布成功
    close.record_review(second["version"]["version_id"], "复核人丁")
    assert close.publish(second["version"]["version_id"])["published"] is True


def test_partial_carrier_rate_resend_keeps_other_carriers(period):
    """承运商只补自己那半张运价表时，其它承运商沿用先前登记的价格。"""
    first = _seed(period)
    _publish(first)
    text = _clean_rates().decode("utf-8").splitlines()
    sf_only = "\n".join(
        line for line in text
        if line.startswith("carrier") or line.startswith("SF,")
    )
    # 抬高版本号，只补 SF
    sf_lines = []
    for line in sf_only.splitlines():
        cells = line.split(",")
        if line.startswith("SF,"):
            cells[-1] = "4"
        sf_lines.append(",".join(cells))
    close.add_rate_source(period, ("\n".join(sf_lines) + "\n").encode("utf-8"), "sf_only.csv")
    second = close.build_version(period)
    assert second["reused"] is False
    assert second["scope"]["carrier_sources"]["SF"] != second["scope"]["carrier_sources"]["JD"]

    carriers = {row["carrier"] for row in store.close_invoice_lines(
        second["version"]["version_id"])}
    assert carriers == {"SF", "JD", "YT"}
    # JD / YT 仍按原价计费，未被 SF 的补发冲掉
    jd = [row for row in store.close_invoice_lines(second["version"]["version_id"])
          if row["waybill_no"] == "WB-1003"][0]
    assert jd["rate_set_version"] == 3
    sf = [row for row in store.close_invoice_lines(second["version"]["version_id"])
          if row["waybill_no"] == "WB-1001"][0]
    assert sf["rate_set_version"] == 4
    assert second["version"]["billed_count"] == first["version"]["billed_count"]


def test_concurrent_resend_and_build_creates_one_version(period):
    """六路同时补发同一份文件并构建：只登记一次、只出一个新版本、账单不重复。"""
    import threading

    first = _seed(period)
    _publish(first)
    results = []

    def worker(index: int) -> None:
        try:
            added = close.add_waybill_source(
                period, close_samples.resend_bytes(), f"resend-{index}.csv",
            )
            built = close.build_version(period)
            results.append(("ok", added["added"], built["reused"],
                            built["version"]["version_id"]))
        except Exception as error:  # noqa: BLE001 - 失败也记下来供断言
            results.append(("err", type(error).__name__))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(item[0] == "ok" for item in results), results
    assert sum(1 for item in results if item[1] is True) == 1
    version_ids = {item[3] for item in results}
    assert len(version_ids) == 1
    versions = store.close_versions(period)
    assert [v["version_no"] for v in versions] == [1, 2]
    assert len(store.close_invoice_lines(versions[-1]["version_id"])) == 15


def test_concurrent_publish_switches_once(period):
    """多路同时发布同一版本：只发生一次真切换，其余返回已发布。"""
    import threading

    first = _seed(period)
    _publish(first)
    close.add_waybill_source(period, close_samples.resend_bytes(), "resend.csv")
    second = close.build_version(period)
    version_id = second["version"]["version_id"]
    results = []

    def worker(index: int) -> None:
        try:
            results.append(close.publish(version_id, f"复核人{index}")["published"])
        except Exception as error:  # noqa: BLE001
            results.append(type(error).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, False, False, False, True]
    assert close.official_version(period)["version_id"] == version_id
    assert store.close_version(first["version"]["version_id"])["state"] == store.CLOSE_SUPERSEDED


def test_resend_can_repair_a_previously_quarantined_waybill(period):
    """补发送来一版合格的脏票：原来隔离的行应转为计费，并列在“新增计费”里。"""
    first = _seed(period)
    _publish(first)
    header = close_samples.HEADER
    fixed = "\n".join([
        header,
        "WB-1007,SF,standard,Z1,2026-04-09T14:30:00+08:00,2,kg,20,20,20,cm,0,1",
    ]) + "\n"
    added = close.add_waybill_source(period, fixed.encode("utf-8"), "fixed.csv")
    assert added["added"] is True

    second = close.build_version(period)
    invoice = {row["waybill_no"]: row for row in store.close_invoice_lines(
        second["version"]["version_id"])}
    assert invoice["WB-1007"]["total_amount"] == "20.62"
    impact = close.impact_report(second["version"]["version_id"])
    added_group = {group["change"]: group for group in impact["groups"]}["added"]
    assert [row["waybill_no"] for row in added_group["waybills"]] == ["WB-1007"]


def test_bad_resend_does_not_destroy_the_good_bill(period):
    """坏补发（同号改成哨兵值）不能让原来合格的票从账单里消失。"""
    first = _seed(period)
    _publish(first)
    header = close_samples.HEADER
    bad = "\n".join([
        header,
        "WB-1002,SF,standard,Z1,2026-04-03T09:40:00+08:00,N/A,kg,10,10,10,cm,0,1",
    ]) + "\n"
    close.add_waybill_source(period, bad.encode("utf-8"), "bad.csv")
    second = close.build_version(period)

    invoice = {row["waybill_no"]: row for row in store.close_invoice_lines(
        second["version"]["version_id"])}
    # 坏行被隔离，首版那份合格值仍是本期账单
    assert invoice["WB-1002"]["total_amount"] == "20.62"
    assert second["version"]["billed_count"] == first["version"]["billed_count"]
    impact = close.impact_report(second["version"]["version_id"])
    assert impact["groups"] == []
    assert any(
        row["waybill_no"] == "WB-1002" and row["code"] == "SENTINEL_VALUE"
        for row in impact["new_quarantine"]
    )
    accounts = {account["key"]: account for account in
                close.version_detail(second["version"]["version_id"])["accounts"]}
    assert all(account["balanced"] is True for account in accounts.values())
