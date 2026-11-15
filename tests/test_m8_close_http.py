"""M8 HTTP 面：页面、正式导出只认正式版本、发布前后切换。"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from fixtures import close_samples

from app import store
from app.main import app

_CLOSE_TABLES = (
    "close_periods", "close_sources", "close_versions", "close_invoice_lines",
    "close_evidence_rows", "close_scope_rows", "close_impacts", "close_manifests",
    "close_reviews", "close_official", "close_rate_cards",
)

PERIOD = "2026-04"


def _read(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def fresh(client):
    with store.session() as conn:
        for table in _CLOSE_TABLES:
            conn.execute(f"DELETE FROM {table}")
    return client


def _seed_close(client) -> dict:
    client.post("/api/close/rates", data={"period": PERIOD})
    client.post("/api/close/waybills", data={"period": PERIOD})
    return client.post("/api/close/build", data={"period": PERIOD}).json()["result"]


def test_close_page_renders(fresh):
    response = fresh.get("/close")
    assert response.status_code == 200
    assert "月结出账" in response.text
    assert "2026-04" in response.text


def test_official_export_refuses_before_publish(fresh):
    _seed_close(fresh)
    for path in ("invoice.csv", "evidence.csv", "manifest.json"):
        response = fresh.get(f"/api/close/official/{path}?period={PERIOD}")
        assert response.status_code == 409, path


def test_full_close_flow_over_http(fresh):
    built = _seed_close(fresh)
    version_id = built["version"]["version_id"]
    assert built["version"]["amount_total"] == "1501.60"

    impact = fresh.get(f"/api/close/versions/{version_id}/impact").json()["impact"]
    assert impact["totals"]["new_total"] == "1501.60"

    reviewed = fresh.post(
        f"/api/close/versions/{version_id}/review",
        data={"reviewer": "复核员甲", "note": "四账已核"},
    )
    assert reviewed.status_code == 200

    published = fresh.post(f"/api/close/versions/{version_id}/publish", data={})
    assert published.status_code == 200
    assert published.json()["result"]["published"] is True

    invoice = fresh.get(f"/api/close/official/invoice.csv?period={PERIOD}")
    assert invoice.status_code == 200
    rows = _read(invoice.text)
    assert len(rows) == 14
    assert {row["period"] for row in rows} == {PERIOD}
    total = sum(__import__("decimal").Decimal(row["total_amount"]) for row in rows)
    assert str(total) == "1501.60"

    manifest = fresh.get(f"/api/close/official/manifest.json?period={PERIOD}").json()
    assert manifest["version_id"] == version_id
    assert manifest["reconcile"]["balanced"] is True


def test_publish_requires_review_via_http(fresh):
    built = _seed_close(fresh)
    response = fresh.post(
        f"/api/close/versions/{built['version']['version_id']}/publish", data={},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "CLOSE_REVIEW_REQUIRED"


def test_resend_then_publish_switches_official_exports(fresh):
    built = _seed_close(fresh)
    first_id = built["version"]["version_id"]
    fresh.post(f"/api/close/versions/{first_id}/review", data={"reviewer": "甲"})
    fresh.post(f"/api/close/versions/{first_id}/publish", data={})
    first_csv = fresh.get(f"/api/close/official/invoice.csv?period={PERIOD}").text

    added = fresh.post(
        "/api/close/waybills",
        data={"period": PERIOD},
        files={"file": ("resend.csv", close_samples.resend_bytes(), "text/csv")},
    )
    assert added.status_code == 200
    assert added.json()["result"]["added"] is True

    second = fresh.post("/api/close/build", data={"period": PERIOD}).json()["result"]
    second_id = second["version"]["version_id"]
    assert second_id != first_id

    # 还没复核发布：正式导出必须仍是旧版，一步都不许提前变
    still = fresh.get(f"/api/close/official/invoice.csv?period={PERIOD}").text
    assert still == first_csv

    fresh.post(f"/api/close/versions/{second_id}/review", data={"reviewer": "乙"})
    fresh.post(f"/api/close/versions/{second_id}/publish", data={})

    after = fresh.get(f"/api/close/official/invoice.csv?period={PERIOD}").text
    assert after != first_csv
    assert len(_read(after)) == 15
    official = fresh.get(f"/api/close/official?period={PERIOD}").json()
    assert official["version_id"] == second_id
    # 旧版本仍可整份导出
    old = fresh.get(f"/api/close/versions/{first_id}/invoice.csv")
    assert old.status_code == 200
    assert old.text == first_csv


def test_repeated_resend_over_http_is_idempotent(fresh):
    built = _seed_close(fresh)
    version_id = built["version"]["version_id"]
    for _ in range(2):
        fresh.post(
            "/api/close/waybills",
            data={"period": PERIOD},
            files={"file": ("resend.csv", close_samples.resend_bytes(), "text/csv")},
        )
    first_rebuild = fresh.post("/api/close/build", data={"period": PERIOD}).json()["result"]
    assert first_rebuild["reused"] is False
    for _ in range(3):
        again = fresh.post("/api/close/build", data={"period": PERIOD}).json()["result"]
        assert again["reused"] is True
        assert again["version"]["version_id"] == first_rebuild["version"]["version_id"]
    assert len([v for v in store.close_versions(PERIOD)]) == 2
    assert store.close_version(version_id)["state"] == store.CLOSE_DRAFT


def test_impact_csv_export(fresh):
    built = _seed_close(fresh)
    version_id = built["version"]["version_id"]
    response = fresh.get(f"/api/close/versions/{version_id}/impact.csv")
    assert response.status_code == 200
    rows = _read(response.text)
    assert len(rows) == 14
    assert {row["change"] for row in rows} == {"added"}


def test_evidence_csv_covers_scope(fresh):
    built = _seed_close(fresh)
    response = fresh.get(f"/api/close/versions/{built['version']['version_id']}/evidence.csv")
    assert response.status_code == 200
    rows = _read(response.text)
    kinds = {row["row_kind"] for row in rows}
    assert {"invoice", "quarantine", "excluded"} <= kinds
    excluded = [row for row in rows if row["row_kind"] == "excluded"]
    assert [row["waybill_no"] for row in excluded] == ["WB-1011"]


def test_bad_period_is_reported_not_silently_empty(fresh):
    response = fresh.get("/api/close?period=2026-13")
    assert response.status_code == 422
    assert response.json()["code"] == "PERIOD_INVALID"

    page = fresh.get("/close?period=2026-13")
    assert page.status_code == 200
    assert "期格式不对" in page.text
