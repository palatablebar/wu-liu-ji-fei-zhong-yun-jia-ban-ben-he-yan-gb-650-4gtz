"""M8 入口：月结页、来源登记、版本构建、影响复核、发布与正式导出。"""

from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from app import config, web
from app.errors import DomainError
from app.modules.m8_close import scope as scope_mod, service

router = APIRouter(tags=["close"])


def _period_or_default(period: str) -> str:
    """空串取当期；给了但不合法则直接 422，避免把“期写错”看成“本期没数据”。"""
    chosen = (period or "").strip() or service.current_period()
    if not scope_mod.valid_period(chosen):
        raise DomainError("PERIOD_INVALID", f"关账期格式应为 YYYY-MM：{chosen}", 422)
    return chosen


async def _upload(file: UploadFile | None, sample: str) -> tuple[bytes, str]:
    if file is not None and file.filename:
        return await file.read(), file.filename
    path = config.SAMPLE_DIR / sample
    return (path.read_bytes() if path.exists() else b""), sample


@router.get("/close", response_class=HTMLResponse)
def page_close(request: Request, period: str = "", version: str = ""):
    context = web.shell(request, active="close")
    context["period_error"] = ""
    try:
        chosen = _period_or_default(period)
    except DomainError as error:
        # 页面不 500：回落到当期并提示，期写错要一眼看见
        context["period_error"] = error.detail
        chosen = service.current_period()
    summary = service.period_summary(chosen)
    # 复核人要看的是“待发布的那个版本”：有草稿看草稿，否则看正式版
    focus = service.version_or_none(version) or service.review_target(chosen)
    context.update({
        "period": chosen,
        "summary": summary,
        "official": summary["official"],
        "effective": focus,
        "samples": {"rate": config.SAMPLE_RATES, "waybills": config.SAMPLE_WAYBILLS},
    })
    effective = focus
    if effective:
        context["impact"] = service.impact_report(effective["version_id"])
        context["accounts"] = service.version_accounts(effective["version_id"])
        vid = effective["version_id"]
        context["export_urls"] = {
            "invoice": f"/api/close/versions/{vid}/invoice.csv",
            "evidence": f"/api/close/versions/{vid}/evidence.csv",
            "impact": f"/api/close/versions/{vid}/impact.csv",
            "manifest": f"/api/close/versions/{vid}/manifest.json",
        }
    return web.page("close.html", context)


@router.get("/api/close")
def api_close(period: str = ""):
    chosen = _period_or_default(period)
    summary = service.period_summary(chosen)
    effective = service.effective_version(chosen)
    payload = {
        "ok": True,
        "period": chosen,
        "summary": summary,
        "official_version_id": summary["official_version_id"],
        "effective_version_id": effective["version_id"] if effective else "",
    }
    if effective:
        payload["impact"] = service.impact_report(effective["version_id"])
    return payload


@router.post("/api/close/rates")
async def api_add_rate(period: str = Form(default=""),
                       file: UploadFile | None = File(default=None),
                       sample: str = Form(default="")):
    chosen = _period_or_default(period)
    raw, name = await _upload(file, sample or config.SAMPLE_RATES)
    return {"ok": True, "period": chosen, "result": service.add_rate_source(chosen, raw, name)}


@router.post("/api/close/waybills")
async def api_add_waybills(period: str = Form(default=""),
                           file: UploadFile | None = File(default=None),
                           sample: str = Form(default="")):
    chosen = _period_or_default(period)
    raw, name = await _upload(file, sample or config.SAMPLE_WAYBILLS)
    return {"ok": True, "period": chosen, "result": service.add_waybill_source(chosen, raw, name)}


@router.post("/api/close/build")
def api_build(period: str = Form(default=""), note: str = Form(default="")):
    chosen = _period_or_default(period)
    result = service.build_version(chosen, note)
    return {"ok": True, "period": chosen, "result": result}


@router.get("/api/close/versions")
def api_versions(period: str = ""):
    chosen = _period_or_default(period)
    return {"ok": True, "period": chosen, "summary": service.period_summary(chosen)}


@router.get("/api/close/versions/{version_id}")
def api_version(version_id: str):
    return {"ok": True, "detail": service.version_detail(version_id)}


@router.get("/api/close/versions/{version_id}/impact")
def api_impact(version_id: str):
    return {"ok": True, "impact": service.impact_report(version_id)}


@router.post("/api/close/versions/{version_id}/review")
def api_review(version_id: str, reviewer: str = Form(default=""), note: str = Form(default="")):
    return {"ok": True, "result": service.record_review(version_id, reviewer, note)}


@router.post("/api/close/versions/{version_id}/publish")
def api_publish(version_id: str, reviewer: str = Form(default=""), note: str = Form(default="")):
    return {"ok": True, "result": service.publish(version_id, reviewer, note)}


@router.get("/api/close/versions/{version_id}/invoice.csv")
def api_invoice(version_id: str):
    return Response(
        content=service.invoice_csv(version_id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="invoice-{version_id}.csv"'},
    )


@router.get("/api/close/versions/{version_id}/evidence.csv")
def api_evidence(version_id: str):
    return Response(
        content=service.evidence_csv(version_id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="evidence-{version_id}.csv"'},
    )


@router.get("/api/close/versions/{version_id}/impact.csv")
def api_impact_csv(version_id: str):
    return Response(
        content=service.impact_csv(version_id),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="impact-{version_id}.csv"'},
    )


@router.get("/api/close/versions/{version_id}/manifest.json")
def api_manifest(version_id: str):
    return Response(
        content=service.manifest_json(version_id),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="manifest-{version_id}.json"'},
    )


@router.get("/api/close/official")
def api_official(period: str = ""):
    chosen = _period_or_default(period)
    version = service.official_version(chosen)
    return {
        "ok": True,
        "period": chosen,
        "version_id": version["version_id"] if version else "",
        "version": version,
    }


@router.get("/api/close/official/invoice.csv")
def api_official_invoice(period: str = ""):
    chosen = _period_or_default(period)
    version = service.official_version(chosen)
    if not version:
        return Response(content="version_id,error\n,NO_OFFICIAL_VERSION\n",
                        media_type="text/csv; charset=utf-8", status_code=409)
    return Response(
        content=service.invoice_csv(version["version_id"]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="official-invoice-{chosen}-v{version["version_no"]}.csv"'},
    )


@router.get("/api/close/official/evidence.csv")
def api_official_evidence(period: str = ""):
    chosen = _period_or_default(period)
    version = service.official_version(chosen)
    if not version:
        return Response(content="version_id,error\n,NO_OFFICIAL_VERSION\n",
                        media_type="text/csv; charset=utf-8", status_code=409)
    return Response(
        content=service.evidence_csv(version["version_id"]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="official-evidence-{chosen}-v{version["version_no"]}.csv"'},
    )


@router.get("/api/close/official/manifest.json")
def api_official_manifest(period: str = ""):
    chosen = _period_or_default(period)
    version = service.official_version(chosen)
    if not version:
        return Response(content='{"ok":false,"code":"NO_OFFICIAL_VERSION"}',
                        media_type="application/json; charset=utf-8", status_code=409)
    return Response(
        content=service.manifest_json(version["version_id"]),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="official-manifest-{chosen}-v{version["version_no"]}.json"'},
    )
