"""探针试跑接口：生成/下载数据包、执行试跑、核对快照来源、核销模板选择器。

放在通用资源路由之前注册：资源路由的 `/{module}/{entry_id}` 会把
`probe-trial/...` 这类两级路径当成模块 ID 吃掉。
"""
from __future__ import annotations

import base64
import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.services.probe_package import (
    PACKAGE_STORE,
    PackageValidationError,
    decode_package,
    package_bytes_from_payload,
)
from app.services.probe_package_builder import build_package, package_summary
from app.services.probe_trial import (
    FAILED,
    LEDGER,
    PENDING,
    RECONCILED,
    TRIALABLE,
    VERIFIED,
    reconcile_template_selectors,
    run_trial,
    selector_verification_report,
    verify_package_bytes,
    verify_snapshot_provenance,
)
from app.store import store

router = APIRouter(prefix="/api/probe-trial", tags=["探针试跑"])


class PackageBuildPayload(BaseModel):
    template_ids: list[int] = Field(default_factory=list)
    all_templates: bool = False
    quota_per_page: int = Field(default=1, ge=1, le=20)
    package_id: str | None = None
    round_id: str | None = None
    include_content: bool = False


class TrialPayload(BaseModel):
    package_id: str | None = None
    package_base64: str | None = None


class VerifyPayload(BaseModel):
    package_id: str | None = None
    package_base64: str | None = None


class TemplateRevisionPayload(BaseModel):
    fields: str | None = None
    selectors: str | None = None
    sample_page: str | None = None
    status: str | None = None


def _package_download(package_id: str) -> Response:
    data = PACKAGE_STORE.load(package_id)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail=f"数据包 {package_id} 不存在或未完整落盘，请重新打包",
        )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{package_id}.zip"',
            "X-Package-Sha256": hashlib.sha256(data).hexdigest(),
        },
    )


@router.post("/packages/preview")
def preview_package(payload: PackageBuildPayload) -> dict[str, Any]:
    """预览候选清单、可试跑/待验证声明和采样额度冲突，不落盘。"""
    if payload.all_templates:
        rows = list(store.rows("template_rule"))
    else:
        if not payload.template_ids:
            raise HTTPException(status_code=400, detail="请至少选择一个模板，或声明 all_templates")
        rows = []
        for template_id in payload.template_ids:
            row = store.find("template_rule", int(template_id))
            if row is None:
                raise HTTPException(status_code=404, detail=f"模板 {template_id} 不存在")
            rows.append(row)
    # 多模板必须放在一起判定额度，否则同一页两份模板的冲突在预览里看不到。
    from app.services.probe_trial import build_round_candidates

    round_data = build_round_candidates(rows, quota_per_page=payload.quota_per_page)
    return {
        "ok": True,
        "quota_per_page": payload.quota_per_page,
        "templates": round_data["templates"],
        "candidates": round_data["candidates"],
        "trialable": round_data["trialable"],
        "pending": round_data["pending"],
        "declaration": round_data["declaration"],
        "sample_pages": round_data["sample_pages"],
        "cross_template_conflicts": round_data["conflicts"],
    }


@router.post("/packages")
def create_package(payload: PackageBuildPayload) -> dict[str, Any]:
    """生成数据包并原子落盘；include_content=true 时同时返回 base64 内容。"""
    try:
        package = build_package(
            template_ids=payload.template_ids,
            all_templates=payload.all_templates,
            quota_per_page=payload.quota_per_page,
            package_id=payload.package_id,
            round_id=payload.round_id,
        )
    except PackageValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    summary = package_summary(package)
    summary["download_url"] = f"/api/probe-trial/packages/{package['package_id']}/download"
    summary["checksums_verified"] = True
    if payload.include_content:
        summary["package_base64"] = base64.b64encode(package["bytes"]).decode("ascii")
    return summary


@router.post("/packages/{package_id}/consume")
def consume_package_as_probe(package_id: str) -> dict[str, Any]:
    """探针选择侧读取数据包触发快照：与试跑同一条链路，但记录来源为 probe_selector。"""
    data = PACKAGE_STORE.load(package_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"数据包 {package_id} 不存在或未完整落盘")
    try:
        decoded = decode_package(data)
    except PackageValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return run_trial(decoded, source="probe_selector")


@router.post("/packages/{package_id}/rebuild")
def rebuild_package(package_id: str, payload: PackageBuildPayload | None = None) -> dict[str, Any]:
    """打包中断后按同一标识重试：覆盖半成品，成功后才重新可见。"""
    body = payload or PackageBuildPayload(package_id=package_id)
    if body.package_id and body.package_id != package_id:
        raise HTTPException(status_code=400, detail="重建时 package_id 必须与路径一致")
    body.package_id = package_id
    return create_package(body)


@router.get("/packages")
def list_packages() -> dict[str, Any]:
    items = []
    for meta in PACKAGE_STORE.list_packages():
        data = PACKAGE_STORE.load(str(meta.get("package_id")))
        items.append({
            **meta,
            "readable": data is not None,
        })
    return {"items": items, "total": len(items)}


@router.get("/packages/{package_id}")
def get_package(package_id: str) -> dict[str, Any]:
    data = PACKAGE_STORE.load(package_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"数据包 {package_id} 不存在或未完整落盘")
    decoded = decode_package(data)
    manifest = decoded["manifest"]
    return {
        "ok": True,
        "package_id": manifest.get("package_id"),
        "package_sha256": decoded["package_sha256"],
        "manifest": manifest,
        "template": decoded["template"],
        "templates": decoded.get("templates", []),
        "candidates": decoded["candidates"],
        "checksums_verified": decoded["checksums_verified"],
        "warnings": decoded["warnings"],
        "trialable": [c for c in decoded["candidates"] if c["status"] == TRIALABLE],
        "pending": [c for c in decoded["candidates"] if c["status"] == PENDING],
        "download_url": f"/api/probe-trial/packages/{package_id}/download",
    }


@router.get("/packages/{package_id}/download")
def download_package(package_id: str) -> Response:
    return _package_download(package_id)


@router.post("/packages/{package_id}/verify")
def verify_stored_package(package_id: str) -> dict[str, Any]:
    data = PACKAGE_STORE.load(package_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"数据包 {package_id} 不存在或未完整落盘")
    try:
        return verify_package_bytes(data)
    except PackageValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/verify")
def verify_uploaded_package(payload: VerifyPayload) -> dict[str, Any]:
    """校验一份上传或已落盘的数据包，不执行试跑。"""
    try:
        data, package_id = package_bytes_from_payload(payload.model_dump())
        if data is None:
            if not package_id:
                raise PackageValidationError("请提供 package_id 或 package_base64")
            data = PACKAGE_STORE.load(package_id)
            if data is None:
                raise HTTPException(
                    status_code=404, detail=f"数据包 {package_id} 不存在或未完整落盘")
        return verify_package_bytes(data)
    except PackageValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/runs")
def create_trial_run(payload: TrialPayload) -> dict[str, Any]:
    """读取数据包触发快照并试跑，结论回写到字段候选清单。"""
    try:
        data, package_id = package_bytes_from_payload(payload.model_dump())
        source = "upload"
        if data is None:
            if not package_id:
                raise PackageValidationError("请提供 package_id 或 package_base64")
            data = PACKAGE_STORE.load(package_id)
            source = "stored"
            if data is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"数据包 {package_id} 不存在或未完整落盘，请重新打包后再试跑",
                )
        decoded = decode_package(data)
        return run_trial(decoded, source=source)
    except PackageValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/runs")
def list_trial_runs() -> dict[str, Any]:
    items = LEDGER.list_runs()
    return {"items": items, "total": len(items)}


@router.get("/runs/{run_id}")
def get_trial_run(run_id: str) -> dict[str, Any]:
    record = LEDGER.get_run(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"试跑记录 {run_id} 不存在")
    return record


@router.get("/field-candidates")
def list_field_candidates(
    template_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """字段候选清单：模板规则据此核销已验证的选择器。"""
    items = LEDGER.list(template_id=template_id, status=status)
    summary = {
        status_name: sum(1 for item in LEDGER.list() if item["status"] == status_name)
        for status_name in (TRIALABLE, PENDING, VERIFIED, FAILED, RECONCILED)
    }
    return {"items": items, "total": len(items), "counts": summary}


@router.get("/snapshots/{snapshot_id}/provenance")
def snapshot_provenance(snapshot_id: int) -> dict[str, Any]:
    """独立核对快照数据的来源：重算哈希并回查数据包文件。"""
    result = verify_snapshot_provenance(snapshot_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "快照不存在"))
    return result


@router.get("/templates/{template_id}/verification")
def template_verification(template_id: int) -> dict[str, Any]:
    report = selector_verification_report(template_id)
    if not report.get("ok"):
        raise HTTPException(status_code=404, detail=report.get("reason"))
    return report


@router.post("/templates/{template_id}/revision")
def revise_template(template_id: int, payload: TemplateRevisionPayload) -> dict[str, Any]:
    """更新模板的字段/选择器/样本页，供“更新模板后再用旧包试跑”的场景使用。"""
    row = store.find("template_rule", template_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"模板 {template_id} 不存在")

    from app.services.probe_trial import canonical_template

    before = canonical_template(row)
    if payload.fields is not None:
        row["提取字段"] = payload.fields
    if payload.selectors is not None:
        row["字段选择器"] = payload.selectors
    if payload.sample_page is not None:
        row["采样页面"] = payload.sample_page
    if payload.status:
        row["status"] = payload.status
    after = canonical_template(row)
    return {
        "ok": True,
        "template_id": template_id,
        "previous_version": before["version"],
        "current_version": after["version"],
        "changed": before["version"] != after["version"],
        "template": after,
    }


@router.post("/templates/{template_id}/reconcile")
def reconcile_template(template_id: int, apply: bool = Query(default=True)) -> dict[str, Any]:
    """把试跑通过的选择器回写到模板规则，完成核销。"""
    report = reconcile_template_selectors(template_id, apply=apply)
    if not report.get("ok"):
        raise HTTPException(status_code=404, detail=report.get("reason"))
    return report
