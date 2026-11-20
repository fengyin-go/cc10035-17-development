"""模板规则模块的探针试跑数据包编排：选模板 → 判额度 → 打包含快照。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services.probe_package import (
    CANDIDATES_NAME,
    CURRENT_SCHEMA_VERSION,
    FIELD_SEMANTICS,
    FORMAT_NAME,
    TEMPLATE_NAME,
    PackageValidationError,
    build_and_save,
    encode_package,
    new_package_id,
    sha256_text,
    utc_now,
)
from app.services.probe_trial import (
    PENDING,
    TRIALABLE,
    build_round_candidates,
    canonical_template,
    template_fields,
    template_sample_pages,
    template_selectors,
)
from app.store import store


def template_row(template_id: int) -> dict[str, Any] | None:
    return store.find("template_rule", template_id)


def _readme(manifest: dict[str, Any]) -> str:
    lines = [
        "探针试跑配置数据包",
        "=" * 24,
        f"数据包标识：{manifest.get('package_id')}",
        f"打包时间：{manifest.get('created_at')}",
        f"模板快照版本：{manifest.get('template_version')}",
        "",
        "本轮实际可试跑的候选：",
    ]
    trialable = manifest.get("declaration", {}).get("trialable", [])
    if not trialable:
        lines.append("  （无）")
    for item in trialable:
        lines.append(
            f"  - {item['field']}  {item['selector']}  样本页：{item['sample_page']}"
            f"  [候选 {item['candidate_id']}]"
        )
    lines.append("")
    lines.append("本轮留下待验证的配置：")
    pending = manifest.get("declaration", {}).get("pending", [])
    if not pending:
        lines.append("  （无）")
    for item in pending:
        lines.append(
            f"  - {item['field']}  {item['selector'] or '（缺选择器）'}"
            f"  样本页：{item['sample_page'] or '（缺样本页）'}  原因：{item['reason']}"
        )
    lines.extend([
        "",
        "文件说明：",
        "  manifest.json    交付声明、模板快照摘要、文件校验和",
        "  template.json    打包时的模板快照（可据此还原当时模板与每个选择器）",
        "  candidates.json  字段候选清单，candidate_id 为稳定标识",
        "  probes.json      同一批候选的探针写法，兼容旧版消费方",
        "  SHA256SUMS       除 manifest.json 外每个文件的 SHA-256",
        "",
        "校验方式：sha256sum -c SHA256SUMS（在解包目录内执行）。",
        f"这些文件的编码与校验清单均在打包时生成；schema_version={manifest.get('schema_version')}。",
        "",
    ])
    return "\n".join(lines)


def build_package(
    *,
    template_ids: list[int] | None = None,
    all_templates: bool = False,
    quota_per_page: int = 1,
    package_id: str | None = None,
    round_id: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """生成探针试跑数据包。返回包内容（bytes）以及交付声明。"""
    if all_templates:
        rows = list(store.rows("template_rule"))
    elif template_ids:
        rows = []
        for template_id in template_ids:
            row = template_row(int(template_id))
            if row is None:
                raise PackageValidationError(f"模板 {template_id} 不存在")
            rows.append(row)
    else:
        raise PackageValidationError("请至少选择一个模板，或声明 all_templates")

    if quota_per_page < 1:
        raise PackageValidationError("quota_per_page 必须为正整数")

    round_data = build_round_candidates(rows, quota_per_page=quota_per_page)
    templates = round_data["templates"]
    candidates = round_data["candidates"]

    package_identifier = package_id or new_package_id()
    created_at = utc_now()
    manifest: dict[str, Any] = {
        "format": FORMAT_NAME,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "package_id": package_identifier,
        "round_id": round_id or package_identifier,
        "created_at": created_at,
        "template_ids": [t["id"] for t in templates],
        "template_names": {str(t["id"]): t["name"] for t in templates},
        "template_version": sha256_text(
            "|".join(sorted(str(t["version"]) for t in templates)))[:16],
        "template": templates[0],
        "templates": templates,
        "sample_pages": round_data["sample_pages"],
        "field_semantics": FIELD_SEMANTICS,
        "declaration": {
            **round_data["declaration"],
            "template_count": len(templates),
            "sample_pages": round_data["sample_pages"],
        },
        "conflicts": round_data["conflicts"],
        "notes": [
            "trialable 可直接交给采集端按 sample_page 试跑",
            "pending 不进入本轮试跑，保留原因供下一轮处理",
            "template.json 是打包时的快照，模板后续修改不影响旧包还原",
        ],
    }

    probes = [
        {
            "candidate_id": candidate["candidate_id"],
            "template_id": candidate["template_id"],
            "probe": candidate["probe"],
            "status": candidate["status"],
            "reason": candidate["reason"],
            "legacy_fields": candidate["legacy_fields"],
        }
        for candidate in candidates
    ]
    readme = _readme(manifest)

    def builder() -> bytes:
        return encode_package(
            manifest=manifest,
            template_doc=templates,
            candidates=candidates,
            probes=probes,
            readme=readme,
        )

    if persist:
        data, meta = build_and_save(package_identifier, builder)
        package_sha256 = meta["sha256"]
    else:
        data = builder()
        package_sha256 = hashlib.sha256(data).hexdigest()

    # 打包时同步把候选清单的初始状态登记到字段候选面板。
    from app.services.probe_trial import LEDGER

    for candidate in candidates:
        if candidate["status"] == TRIALABLE:
            LEDGER.upsert_pending({**candidate, "status": PENDING, "reason": ""}, package_identifier)
        else:
            LEDGER.upsert_pending(candidate, package_identifier)

    return {
        "ok": True,
        "package_id": package_identifier,
        "package_sha256": package_sha256,
        "bytes": data,
        "manifest": manifest,
        "template": templates[0],
        "templates": templates,
        "candidates": candidates,
        "declaration": manifest["declaration"],
        "conflicts": round_data["conflicts"],
        "trialable": round_data["trialable"],
        "pending": round_data["pending"],
        "file_names": [TEMPLATE_NAME, CANDIDATES_NAME],
    }


def package_summary(package: dict[str, Any]) -> dict[str, Any]:
    """给前端展示的摘要，不包含二进制内容。"""
    manifest = package.get("manifest") or {}
    return {
        "ok": True,
        "package_id": package.get("package_id"),
        "package_sha256": package.get("package_sha256"),
        "created_at": manifest.get("created_at"),
        "schema_version": manifest.get("schema_version"),
        "template_ids": manifest.get("template_ids"),
        "template_names": manifest.get("template_names"),
        "sample_pages": manifest.get("sample_pages"),
        "declaration": package.get("declaration"),
        "conflicts": package.get("conflicts"),
        "candidates": package.get("candidates"),
        "templates": package.get("templates"),
        "trialable": package.get("trialable"),
        "pending": package.get("pending"),
        "field_semantics": manifest.get("field_semantics"),
    }


def template_package_preview(template_id: int, quota_per_page: int = 1) -> dict[str, Any]:
    """单模板的候选与额度预览，不落盘。多模板的交叉冲突请走 build_round_candidates。"""
    row = template_row(template_id)
    if row is None:
        return {"ok": False, "reason": f"模板 {template_id} 不存在"}
    round_data = build_round_candidates([row], quota_per_page=quota_per_page)
    return {
        "ok": True,
        "template": canonical_template(row),
        "template_fields": template_fields(row),
        "template_selectors": template_selectors(row),
        "sample_pages": template_sample_pages(row),
        "declaration": round_data["declaration"],
        "candidates": round_data["candidates"],
        "trialable": round_data["trialable"],
        "pending": round_data["pending"],
        "conflicts": round_data["conflicts"],
    }
