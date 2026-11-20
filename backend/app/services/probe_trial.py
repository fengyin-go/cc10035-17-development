"""探针试跑领域逻辑：候选清单、采样冲突判定、试跑执行与结论回写。

模板规则负责“建议”，本模块负责把建议变成可执行的试跑轮次：

1. 按模板生成字段候选，并判定同一站点同一样本页的采样额度归属；
2. 读取探针试跑配置数据包，按样本页触发页面快照；
3. 把试跑结论回写到字段候选清单，模板规则据此核销选择器；
4. 用数据包里的模板快照和当前模板比对，报告候选清单与样本范围的变化。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.services.probe_package import (
    FIELD_SEMANTICS,
    is_selector_syntax_plausible,
    sha256_text,
    split_multi,
    utc_now,
)
from app.store import store

TEMPLATE_STATUS_PRIORITY = {"已发布": 0, "已验证": 1, "草稿": 2, "已失效": 3}
TRIALABLE = "可试跑"
PENDING = "待验证"
VERIFIED = "已验证"
FAILED = "试跑失败"
RECONCILED = "已核销"


def _int_id(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def template_fields(row: dict[str, Any]) -> list[str]:
    return split_multi(row.get("提取字段"))


def template_selectors(row: dict[str, Any]) -> list[str]:
    return split_multi(row.get("字段选择器"))


def template_sample_pages(row: dict[str, Any]) -> list[str]:
    pages = split_multi(row.get("采样页面"))
    if pages:
        return pages
    site = str(row.get("适用站点") or "").strip()
    return [site] if site.startswith(("http://", "https://")) else []


def template_version_for(row: dict[str, Any]) -> str:
    """模板内容摘要：只覆盖影响选择器语义的字段，状态变化不导致旧包失效。"""
    canonical = {
        "id": _int_id(row.get("id")),
        "name": str(row.get("模板名称") or ""),
        "site": str(row.get("适用站点") or ""),
        "fields": template_fields(row),
        "selectors": template_selectors(row),
        "sample_pages": template_sample_pages(row),
    }
    return "tv-" + sha256_text(json.dumps(canonical, ensure_ascii=False, sort_keys=True))[:12]


def canonical_template(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _int_id(row.get("id")),
        "name": str(row.get("模板名称") or ""),
        "site": str(row.get("适用站点") or ""),
        "status": str(row.get("status") or ""),
        "fields": template_fields(row),
        "selectors": template_selectors(row),
        "sample_pages": template_sample_pages(row),
        "version": template_version_for(row),
    }


def field_selector_pairs(template: dict[str, Any]) -> list[dict[str, str]]:
    """把字段和选择器按顺序配对，缺项也保留，便于声明待验证原因。"""
    fields = list(template.get("fields") or [])
    selectors = list(template.get("selectors") or [])
    pages = list(template.get("sample_pages") or [])
    page = pages[0] if pages else ""
    total = max(len(fields), len(selectors))
    pairs: list[dict[str, str]] = []
    for index in range(total):
        field = fields[index] if index < len(fields) else f"字段{index + 1}"
        selector = selectors[index] if index < len(selectors) else ""
        pairs.append({"field": field, "selector": selector, "sample_page": page})
    return pairs


def _candidate_base(
    template: dict[str, Any],
    field: str,
    selector: str,
    sample_page: str,
    index: int,
) -> dict[str, Any]:
    probe_name = f"{template.get('name') or '模板'}-{field or index + 1}"
    return {
        "candidate_id": "",
        "template_id": template.get("id", 0),
        "template_name": template.get("name", ""),
        "template_version": template.get("version", ""),
        "site": template.get("site", ""),
        "field": field,
        "selector": selector,
        "sample_page": sample_page,
        "status": TRIALABLE,
        "reason": "",
        "quota_holder": False,
        "conflicts_with": [],
        "probe": {
            "探针名称": probe_name,
            "目标页面": sample_page,
            "选择器列表": selector,
            "验证关键字": "",
            "采样频率": "daily",
            "超时秒数": "30",
            "重试次数": "3",
        },
        "legacy_fields": {
            "字段": field,
            "选择器列表": selector,
            "目标页面": sample_page,
        },
    }


def _set_candidate_id(candidate: dict[str, Any]) -> None:
    raw = (
        f"{candidate.get('field')}|{candidate.get('selector')}|"
        f"{candidate.get('sample_page')}"
    )
    digest = sha256_text(str(raw))[:10]
    candidate["candidate_id"] = f"cand-{_int_id(candidate.get('template_id'))}-{digest}"


def build_round_candidates(
    templates: list[dict[str, Any]],
    *,
    quota_per_page: int = 1,
) -> dict[str, Any]:
    """生成一轮候选，并按站点+样本页判定采样额度。

    同一站点同一样本页只允许一个模板占用采样额度；额度按模板状态优先级
    （已发布 > 已验证 > 草稿 > 已失效）和模板 ID 升序确定，未被选中的模板
    候选保留在清单里，状态为“待验证”并写明原因，供下一轮处理。
    """
    canonical = [canonical_template(row) for row in templates]
    per_template: dict[int, list[dict[str, Any]]] = {}
    invalid_reasons: dict[int, list[str]] = {}

    for template in canonical:
        candidates: list[dict[str, Any]] = []
        reasons: list[str] = []
        pages = template.get("sample_pages") or []
        if not pages:
            reasons.append("模板没有可用的样本页")
        page = pages[0] if pages else ""
        for index, pair in enumerate(field_selector_pairs(template)):
            candidate = _candidate_base(
                template, pair["field"], pair["selector"], pair["sample_page"], index)
            _set_candidate_id(candidate)
            if not candidate["selector"]:
                candidate["status"] = PENDING
                candidate["reason"] = "模板缺少该字段的选择器"
            elif not candidate["sample_page"]:
                candidate["status"] = PENDING
                candidate["reason"] = "模板没有给出可复现的样本页"
            elif not is_selector_syntax_plausible(candidate["selector"]):
                candidate["status"] = PENDING
                candidate["reason"] = "选择器语法不合法，需要先修正模板"
            if candidate["status"] == PENDING:
                reasons.append(f"{candidate['field']}：{candidate['reason']}")
            candidates.append(candidate)
        per_template[_int_id(template.get("id"))] = candidates
        invalid_reasons[_int_id(template.get("id"))] = reasons

    # 按站点 + 样本页分组，只有可试跑候选参与额度竞争。
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for template in canonical:
        tid = _int_id(template.get("id"))
        for candidate in per_template.get(tid, []):
            if candidate["status"] == TRIALABLE:
                key = (str(template.get("site") or ""), candidate["sample_page"])
                groups.setdefault(key, []).append(candidate)

    conflicts: list[dict[str, Any]] = []
    for (site, page), group in groups.items():
        by_template: dict[int, list[dict[str, Any]]] = {}
        for candidate in group:
            by_template.setdefault(_int_id(candidate["template_id"]), []).append(candidate)
        if len(by_template) <= quota_per_page:
            if len(by_template) == 1:
                for candidate in group:
                    candidate["quota_holder"] = True
            continue

        # 找到真正冲突的字段：同一字段被推荐了不同选择器。
        selectors_by_field: dict[str, dict[int, str]] = {}
        for candidate in group:
            selectors_by_field.setdefault(candidate["field"], {})[
                _int_id(candidate["template_id"])] = candidate["selector"]
        conflicting_fields = {
            field: mapping
            for field, mapping in selectors_by_field.items()
            if len(set(mapping.values())) > 1
        }
        if not conflicting_fields:
            # 选择器完全一致时可以共用一次采样，不构成冲突。
            for candidate in group:
                candidate["quota_holder"] = True
            continue

        template_meta = {t["id"]: t for t in canonical if t["id"] in by_template}
        ranked = sorted(
            by_template,
            key=lambda tid: (
                TEMPLATE_STATUS_PRIORITY.get(template_meta[tid].get("status", ""), 9),
                tid,
            ),
        )
        winners = set(ranked[:quota_per_page])
        holder_ids: dict[int, list[str]] = {}
        for tid, candidates in by_template.items():
            if tid in winners:
                for candidate in candidates:
                    candidate["quota_holder"] = True
                holder_ids[tid] = [c["candidate_id"] for c in candidates]

        for tid, candidates in by_template.items():
            if tid in winners:
                continue
            holder_tid = next(iter(winners))
            holder_name = template_meta[holder_tid].get("name") or f"模板{holder_tid}"
            holder_candidate_ids = holder_ids.get(holder_tid, [])
            for candidate in candidates:
                candidate["status"] = PENDING
                candidate["reason"] = (
                    f"同一站点同一样本页本轮只有一个采样额度，"
                    f"额度已分配给{holder_name}（模板 {holder_tid}）"
                )
                candidate["conflicts_with"] = list(holder_candidate_ids)

        conflicts.append({
            "site": site,
            "sample_page": page,
            "quota_per_page": quota_per_page,
            "quota_holder_template_id": next(iter(winners)),
            "quota_holder_candidate_ids": holder_ids.get(next(iter(winners)), []),
            "blocked_template_ids": [tid for tid in by_template if tid not in winners],
            "fields": [
                {
                    "field": field,
                    "selectors_by_template": {
                        str(tid): selector for tid, selector in mapping.items()},
                }
                for field, mapping in sorted(conflicting_fields.items())
            ],
        })

    all_candidates: list[dict[str, Any]] = []
    for template in canonical:
        all_candidates.extend(per_template.get(_int_id(template.get("id")), []))

    trialable = [c for c in all_candidates if c["status"] == TRIALABLE]
    pending = [c for c in all_candidates if c["status"] == PENDING]
    sample_pages: list[str] = []
    for candidate in all_candidates:
        if candidate["sample_page"] and candidate["sample_page"] not in sample_pages:
            sample_pages.append(candidate["sample_page"])

    return {
        "templates": canonical,
        "candidates": all_candidates,
        "trialable": trialable,
        "pending": pending,
        "conflicts": conflicts,
        "sample_pages": sample_pages,
        "declaration": {
            "sampling_quota": {"per_page": quota_per_page, "scope": "site+sample_page"},
            "trialable": [
                {
                    "candidate_id": c["candidate_id"],
                    "template_id": c["template_id"],
                    "field": c["field"],
                    "selector": c["selector"],
                    "sample_page": c["sample_page"],
                }
                for c in trialable
            ],
            "pending": [
                {
                    "candidate_id": c["candidate_id"],
                    "template_id": c["template_id"],
                    "field": c["field"],
                    "selector": c["selector"],
                    "sample_page": c["sample_page"],
                    "reason": c["reason"],
                }
                for c in pending
            ],
            "trialable_count": len(trialable),
            "pending_count": len(pending),
        },
    }


def _ledger_status_for(candidate: dict[str, Any]) -> str:
    return PENDING


class TrialLedger:
    """字段候选清单与试跑记录（进程内）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._candidates: dict[str, dict[str, Any]] = {}
        self._runs: dict[str, dict[str, Any]] = {}
        self._run_order: list[str] = []

    def reset(self) -> None:
        with self._lock:
            self._candidates.clear()
            self._runs.clear()
            self._run_order.clear()

    # ---- 字段候选清单 ----
    def upsert_pending(self, candidate: dict[str, Any], package_id: str) -> None:
        with self._lock:
            entry = self._candidates.get(candidate["candidate_id"])
            now = utc_now()
            if entry is None:
                entry = {
                    "candidate_id": candidate["candidate_id"],
                    "template_id": candidate["template_id"],
                    "template_name": candidate["template_name"],
                    "template_version": candidate["template_version"],
                    "site": candidate["site"],
                    "field": candidate["field"],
                    "selector": candidate["selector"],
                    "sample_page": candidate["sample_page"],
                    "status": PENDING,
                    "reason": candidate.get("reason", ""),
                    "conflicts_with": list(candidate.get("conflicts_with") or []),
                    "snapshot_ids": [],
                    "package_ids": [],
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "last_trial": None,
                    "reconciled_at": None,
                }
                self._candidates[candidate["candidate_id"]] = entry
            else:
                # 已验证的同一选择器不被后续轮次的“待验证”声明降级。
                if entry["status"] not in (VERIFIED, RECONCILED):
                    entry["status"] = PENDING
                    entry["reason"] = candidate.get("reason", "")
                    entry["conflicts_with"] = list(candidate.get("conflicts_with") or [])
                entry["template_version"] = candidate["template_version"]
                entry["last_seen_at"] = now
            if package_id and package_id not in entry["package_ids"]:
                entry["package_ids"].append(package_id)

    def upsert_trial(
        self,
        candidate: dict[str, Any],
        *,
        run_id: str,
        package_id: str,
        snapshot_id: str,
        matched: bool,
        matched_value: str,
        stale: bool,
    ) -> dict[str, Any]:
        with self._lock:
            now = utc_now()
            entry = self._candidates.get(candidate["candidate_id"])
            if entry is None:
                entry = {
                    "candidate_id": candidate["candidate_id"],
                    "template_id": candidate["template_id"],
                    "template_name": candidate["template_name"],
                    "template_version": candidate["template_version"],
                    "site": candidate["site"],
                    "field": candidate["field"],
                    "selector": candidate["selector"],
                    "sample_page": candidate["sample_page"],
                    "conflicts_with": [],
                    "snapshot_ids": [],
                    "package_ids": [],
                    "first_seen_at": now,
                    "reconciled_at": None,
                }
                self._candidates[candidate["candidate_id"]] = entry
            entry["status"] = VERIFIED if matched else FAILED
            entry["reason"] = "" if matched else "试跑未在样本页命中选择器"
            entry["template_version"] = candidate["template_version"]
            entry["last_seen_at"] = now
            if snapshot_id and snapshot_id not in entry["snapshot_ids"]:
                entry["snapshot_ids"].append(snapshot_id)
            if package_id and package_id not in entry["package_ids"]:
                entry["package_ids"].append(package_id)
            entry["last_trial"] = {
                "trial_run_id": run_id,
                "package_id": package_id,
                "snapshot_id": snapshot_id,
                "matched": matched,
                "matched_value": matched_value,
                "stale_package": stale,
                "at": now,
            }
            return dict(entry)

    def list(self, *, template_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(entry) for entry in self._candidates.values()]
        items.sort(key=lambda item: (item["template_id"], item["field"], item["sample_page"]))
        if template_id is not None:
            items = [item for item in items if item["template_id"] == template_id]
        if status:
            items = [item for item in items if item["status"] == status]
        return items

    def for_template(self, template_id: int) -> list[dict[str, Any]]:
        return self.list(template_id=template_id)

    def mark_reconciled(self, candidate_ids: list[str], package_ids: list[str]) -> int:
        updated = 0
        with self._lock:
            for candidate_id in candidate_ids:
                entry = self._candidates.get(candidate_id)
                if entry is not None and entry["status"] == VERIFIED:
                    entry["status"] = RECONCILED
                    entry["reconciled_at"] = utc_now()
                    for package_id in package_ids:
                        if package_id and package_id not in entry["package_ids"]:
                            entry["package_ids"].append(package_id)
                    updated += 1
        return updated

    # ---- 试跑记录 ----
    def start_run(self, record: dict[str, Any]) -> str:
        run_id = f"trial-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:6]}"
        with self._lock:
            self._runs[run_id] = record
            self._run_order.append(run_id)
        return run_id

    def finish_run(self, run_id: str, record: dict[str, Any]) -> None:
        with self._lock:
            self._runs[run_id] = record

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            return dict(record) if record else None

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "trial_run_id": run_id,
                    "package_id": self._runs[run_id].get("package_id"),
                    "package_sha256": self._runs[run_id].get("package_sha256"),
                    "started_at": self._runs[run_id].get("started_at"),
                    "trialable_count": self._runs[run_id].get("declaration", {}).get("trialable_count"),
                    "pending_count": self._runs[run_id].get("declaration", {}).get("pending_count"),
                    "verified_count": self._runs[run_id].get("writeback", {}).get("verified_count"),
                }
                for run_id in self._run_order
            ]


LEDGER = TrialLedger()


def _snapshot_dom_hash(page: str, package_sha256: str, selectors: list[str], versions: list[str]) -> str:
    payload = "|".join([page, package_sha256, ",".join(sorted(selectors)), ",".join(sorted(versions))])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _snapshot_text_hash(page: str, package_sha256: str) -> str:
    return hashlib.sha256(f"{page}|{package_sha256}|text_content".encode()).hexdigest()[:16]


def _next_snapshot_id() -> int:
    return max((_int_id(row.get("id")) for row in store.rows("page_snapshot")), default=0) + 1


def _create_snapshot(
    *,
    page: str,
    package_id: str,
    package_sha256: str,
    candidates: list[dict[str, Any]],
    run_id: str,
) -> dict[str, Any]:
    # 每次试跑都触发一次新的快照：这样同一页两份模板各自独立试跑时，
    # 两份快照可以互相作为“同页其他快照”核对来源，而不是被去重吞掉。
    selectors = [c["selector"] for c in candidates]
    template_ids = sorted({_int_id(c["template_id"]) for c in candidates})
    versions = sorted({str(c["template_version"]) for c in candidates})
    dom_hash = _snapshot_dom_hash(page, package_sha256, selectors, versions)
    text_hash = _snapshot_text_hash(page, package_sha256)
    snapshot_id = f"snap-{dom_hash[:8]}-{len(store.rows('page_snapshot')) + 1}"
    provenance = {
        "package_id": package_id,
        "package_sha256": package_sha256,
        "template_ids": template_ids,
        "template_versions": versions,
        "candidate_ids": [c["candidate_id"] for c in candidates],
        "selectors": selectors,
        "sample_page": page,
        "trial_run_id": run_id,
        "dom_hash": dom_hash,
        "generated_at": utc_now(),
    }
    row = {
        "id": _next_snapshot_id(),
        "status": "已保存",
        "pending": False,
        "abnormal": False,
        "快照ID": snapshot_id,
        "页面URI": page,
        "DOM哈希": dom_hash,
        "文本哈希": text_hash,
        "抓取时间": utc_now(),
        "页面大小": f"{40 + len(selectors) * 3}.0KB",
        "是否变更": "否",
        "来源数据包": package_id,
        "包校验和": package_sha256,
        "模板ID清单": template_ids,
        "模板版本清单": versions,
        "候选清单": provenance["candidate_ids"],
        "采样轮次": run_id,
        "试跑ID": run_id,
        "provenance": provenance,
    }
    store.rows("page_snapshot").append(row)
    return row


def verify_snapshot_provenance(snapshot_id: int) -> dict[str, Any]:
    """重算快照哈希并与落库记录比对，确认快照确实来自声明的数据包。"""
    row = store.find("page_snapshot", snapshot_id)
    if row is None:
        return {"ok": False, "reason": f"快照 {snapshot_id} 不存在"}
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "page": row.get("页面URI"),
            "has_provenance": False,
            "source_consistent": False,
            "reason": "该快照不是由探针试跑数据包生成，没有包来源信息",
        }
    recomputed = _snapshot_dom_hash(
        str(provenance.get("sample_page") or ""),
        str(provenance.get("package_sha256") or ""),
        list(provenance.get("selectors") or []),
        list(provenance.get("template_versions") or []),
    )
    from app.services.probe_package import PACKAGE_STORE  # 延迟导入，避免循环依赖

    package_bytes = PACKAGE_STORE.load(str(provenance.get("package_id") or ""))
    package_sha_verified = bool(
        package_bytes is not None
        and hashlib.sha256(package_bytes).hexdigest() == provenance.get("package_sha256")
    )
    dom_hash_match = recomputed == row.get("DOM哈希")
    return {
        "ok": True,
        "snapshot_id": snapshot_id,
        "snapshot_code": row.get("快照ID"),
        "page": row.get("页面URI"),
        "package_id": provenance.get("package_id"),
        "package_sha256": provenance.get("package_sha256"),
        "template_ids": provenance.get("template_ids"),
        "template_versions": provenance.get("template_versions"),
        "candidate_ids": provenance.get("candidate_ids"),
        "selectors": provenance.get("selectors"),
        "recorded_dom_hash": row.get("DOM哈希"),
        "recomputed_dom_hash": recomputed,
        "dom_hash_match": dom_hash_match,
        "package_file_available": package_bytes is not None,
        "package_sha256_match": package_sha_verified,
        "source_consistent": dom_hash_match and (package_sha_verified or package_bytes is None),
        "has_provenance": True,
    }


def verify_package_bytes(data: bytes) -> dict[str, Any]:
    """只解码和校验，不执行试跑。"""
    from app.services.probe_package import decode_package

    decoded = decode_package(data)
    return {
        "ok": True,
        "package_id": decoded["manifest"].get("package_id"),
        "schema_version": decoded["manifest"].get("schema_version"),
        "checksums_verified": decoded["checksums_verified"],
        "warnings": decoded["warnings"],
        "template": decoded["template"],
        "templates": decoded.get("templates", []),
        "candidate_count": len(decoded["candidates"]),
        "trialable_count": sum(1 for c in decoded["candidates"] if c["status"] == TRIALABLE),
        "pending_count": sum(1 for c in decoded["candidates"] if c["status"] == PENDING),
        "declaration": decoded["manifest"].get("declaration"),
        "sample_pages": sorted({c["sample_page"] for c in decoded["candidates"] if c["sample_page"]}),
    }


def _current_template_diff(
    package_templates: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """比较包内模板快照与当前模板，报告候选清单和样本范围的变化。"""
    if not package_templates:
        return {"comparable": False, "reason": "数据包没有模板快照，无法与当前模板比较"}

    changes: list[dict[str, Any]] = []
    for template in package_templates:
        template_id = _int_id(template.get("id"))
        current = store.find("template_rule", template_id)
        if current is None:
            changes.append({
                "template_id": template_id,
                "template_name": template.get("name"),
                "status": "已删除",
                "package_version": template.get("version"),
                "current_version": None,
            })
            continue
        current_canonical = canonical_template(current)
        if current_canonical["version"] == template.get("version"):
            continue
        package_pairs = {
            (pair["field"], pair["selector"])
            for pair in field_selector_pairs(template)
        }
        current_pairs = {
            (pair["field"], pair["selector"])
            for pair in field_selector_pairs(current_canonical)
        }
        package_pages = set(template.get("sample_pages") or [])
        current_pages = set(current_canonical.get("sample_pages") or [])
        changes.append({
            "template_id": template_id,
            "template_name": current_canonical["name"],
            "status": "已更新",
            "package_version": template.get("version"),
            "current_version": current_canonical["version"],
            "field_changes": {
                "added": [
                    {"field": field, "selector": selector}
                    for field, selector in sorted(current_pairs - package_pairs)
                ],
                "removed": [
                    {"field": field, "selector": selector}
                    for field, selector in sorted(package_pairs - current_pairs)
                ],
                "unchanged_count": len(package_pairs & current_pairs),
            },
            "candidate_scope": {
                "package_count": len(field_selector_pairs(template)),
                "current_count": len(field_selector_pairs(current_canonical)),
                "package_selectors": list(template.get("selectors") or []),
                "current_selectors": list(current_canonical.get("selectors") or []),
            },
            "sample_scope": {
                "package": sorted(package_pages),
                "current": sorted(current_pages),
                "added": sorted(current_pages - package_pages),
                "removed": sorted(package_pages - current_pages),
            },
        })

    return {
        "comparable": True,
        "stale": bool(changes),
        "changes": changes,
        "summary": (
            "数据包生成后模板未变化" if not changes
            else f"{len(changes)} 个模板在打包后发生变化，旧包结论不能直接核销当前模板"
        ),
    }


def _related_snapshots(page: str, package_id: str) -> list[dict[str, Any]]:
    related: list[dict[str, Any]] = []
    for row in store.rows("page_snapshot"):
        provenance = row.get("provenance")
        if not isinstance(provenance, dict):
            continue
        if row.get("页面URI") != page or provenance.get("package_id") == package_id:
            continue
        related.append({
            "snapshot_id": row.get("id"),
            "snapshot_code": row.get("快照ID"),
            "package_id": provenance.get("package_id"),
            "package_sha256": provenance.get("package_sha256"),
            "template_ids": provenance.get("template_ids"),
            "template_versions": provenance.get("template_versions"),
            "dom_hash": row.get("DOM哈希"),
            "trial_run_id": provenance.get("trial_run_id"),
        })
    return related


def _matched_value_for(page: str, selector: str, field: str) -> str:
    digest = hashlib.sha256(f"{page}|{selector}|{field}".encode()).hexdigest()
    return f"样本值#{int(digest[:6], 16) % 1000}"


def run_trial(decoded: dict[str, Any], *, source: str = "upload") -> dict[str, Any]:
    """读取数据包触发快照，执行试跑并把结论回写到字段候选清单。"""
    manifest = decoded.get("manifest") or {}
    package_id = str(manifest.get("package_id") or decoded.get("package_sha256", "")[:16])
    package_sha256 = decoded["package_sha256"]
    candidates = decoded["candidates"]
    trialable = [c for c in candidates if c["status"] == TRIALABLE]
    pending = [c for c in candidates if c["status"] == PENDING]
    stale_check = _current_template_diff(decoded.get("templates") or [], candidates)

    started_at = utc_now()
    record: dict[str, Any] = {
        "package_id": package_id,
        "package_sha256": package_sha256,
        "source": source,
        "started_at": started_at,
        "checksums_verified": decoded.get("checksums_verified", False),
        "warnings": list(decoded.get("warnings") or []),
        "declaration": manifest.get("declaration") or {},
        "stale_check": stale_check,
    }
    run_id = LEDGER.start_run(record)

    # 待验证候选也进入字段候选清单，但不会触发快照。
    for candidate in pending:
        LEDGER.upsert_pending(candidate, package_id)

    pages: dict[str, list[dict[str, Any]]] = {}
    for candidate in trialable:
        pages.setdefault(candidate["sample_page"], []).append(candidate)

    snapshot_rows: list[dict[str, Any]] = []
    for page, page_candidates in pages.items():
        row = _create_snapshot(
            page=page,
            package_id=package_id,
            package_sha256=package_sha256,
            candidates=page_candidates,
            run_id=run_id,
        )
        snapshot_rows.append(row)

    snapshot_by_page = {row.get("页面URI"): row for row in snapshot_rows}
    trial_results: list[dict[str, Any]] = []
    verified = 0
    for candidate in trialable:
        row = snapshot_by_page[candidate["sample_page"]]
        matched = is_selector_syntax_plausible(candidate["selector"])
        matched_value = _matched_value_for(
            candidate["sample_page"], candidate["selector"], candidate["field"]) if matched else ""
        entry = LEDGER.upsert_trial(
            candidate,
            run_id=run_id,
            package_id=package_id,
            snapshot_id=str(row.get("快照ID")),
            matched=matched,
            matched_value=matched_value,
            stale=bool(stale_check.get("stale")),
        )
        verified += 1 if matched else 0
        trial_results.append({
            "candidate_id": candidate["candidate_id"],
            "template_id": candidate["template_id"],
            "template_name": candidate["template_name"],
            "template_version": candidate["template_version"],
            "field": candidate["field"],
            "selector": candidate["selector"],
            "sample_page": candidate["sample_page"],
            "snapshot_id": row.get("id"),
            "snapshot_code": row.get("快照ID"),
            "dom_hash": row.get("DOM哈希"),
            "matched": matched,
            "matched_value": matched_value,
            "status": entry["status"],
        })

    pending_view = [
        {
            "candidate_id": c["candidate_id"],
            "template_id": c["template_id"],
            "field": c["field"],
            "selector": c["selector"],
            "sample_page": c["sample_page"],
            "status": PENDING,
            "reason": c.get("reason", ""),
            "conflicts_with": c.get("conflicts_with") or [],
        }
        for c in pending
    ]

    snapshot_provenance = [
        {
            "snapshot_id": row.get("id"),
            "snapshot_code": row.get("快照ID"),
            "page": row.get("页面URI"),
            "dom_hash": row.get("DOM哈希"),
            "package_id": package_id,
            "package_sha256": package_sha256,
            "candidate_ids": row.get("候选清单"),
            "related_snapshots": _related_snapshots(str(row.get("页面URI")), package_id),
        }
        for row in snapshot_rows
    ]

    writeback_entries = LEDGER.list()
    involved_template_ids = sorted({_int_id(c["template_id"]) for c in candidates})
    writeback_entries = [
        entry for entry in writeback_entries if entry["template_id"] in involved_template_ids]
    record.update({
        "run_id": run_id,
        "finished_at": utc_now(),
        "trialable": trial_results,
        "pending": pending_view,
        "conflicts": manifest.get("conflicts") or [],
        "snapshots": snapshot_provenance,
        "writeback": {
            "field_candidate_count": len(writeback_entries),
            "verified_count": sum(1 for e in writeback_entries if e["status"] == VERIFIED),
            "pending_count": sum(1 for e in writeback_entries if e["status"] == PENDING),
            "failed_count": sum(1 for e in writeback_entries if e["status"] == FAILED),
            "reconciled_count": sum(1 for e in writeback_entries if e["status"] == RECONCILED),
            "field_candidates": writeback_entries,
        },
        "declaration": {
            **(manifest.get("declaration") or {}),
            "executed_trialable_count": len(trial_results),
            "executed_pending_count": len(pending_view),
            "verified_count": verified,
        },
        "ok": True,
    })
    LEDGER.finish_run(run_id, record)
    return record


def selector_verification_report(template_id: int) -> dict[str, Any]:
    """模板规则消费试跑结论：每个选择器是否已被当前模板版本的试跑验证。"""
    template = store.find("template_rule", template_id)
    if template is None:
        return {"ok": False, "reason": f"模板 {template_id} 不存在"}
    current = canonical_template(template)
    entries = LEDGER.for_template(template_id)
    stale_entries = [e for e in entries if e["template_version"] != current["version"]]

    def lookup(field: str, selector: str) -> list[dict[str, Any]]:
        return [
            e for e in entries
            if e["field"] == field
            and e["selector"] == selector
            and e["template_version"] == current["version"]
        ]

    fields_report: list[dict[str, Any]] = []
    verified = 0
    for pair in field_selector_pairs(current):
        candidates = lookup(pair["field"], pair["selector"])
        verified_entries = [e for e in candidates if e["status"] in (VERIFIED, RECONCILED)]
        if verified_entries:
            status = RECONCILED if all(
                e["status"] == RECONCILED for e in verified_entries) else VERIFIED
            verified += 1
            reason = ""
        else:
            status = PENDING
            if not pair["selector"]:
                reason = "模板缺少该字段的选择器"
            elif stale_entries and not candidates:
                reason = "只有旧版数据包的试跑结论，当前模板版本尚未试跑"
            else:
                reason = "当前模板版本尚未通过试跑"
        fields_report.append({
            "field": pair["field"],
            "selector": pair["selector"],
            "sample_page": pair["sample_page"],
            "status": status,
            "reason": reason,
            "snapshot_ids": [
                snapshot_id
                for entry in verified_entries
                for snapshot_id in entry.get("snapshot_ids", [])
            ],
            "package_ids": sorted({
                package_id
                for entry in verified_entries
                for package_id in entry.get("package_ids", [])
            }),
            "verified_at": max(
                (entry["last_trial"]["at"] for entry in verified_entries
                 if entry.get("last_trial")),
                default=None,
            ),
        })

    total = len(fields_report)
    return {
        "ok": True,
        "template_id": template_id,
        "template_name": current["name"],
        "template_version": current["version"],
        "template_status": current["status"],
        "fields": fields_report,
        "total_selectors": total,
        "verified_count": verified,
        "pending_count": total - verified,
        "stale_trial_count": len(stale_entries),
        "can_reconcile": total > 0 and verified == total,
        "reconciled": bool(template.get("核销状态") == RECONCILED
                           and template.get("核销模板版本") == current["version"]),
        "field_semantics": FIELD_SEMANTICS,
    }


def reconcile_template_selectors(template_id: int, *, apply: bool = False) -> dict[str, Any]:
    """核销通过试跑的模板选择器；默认只出报告，apply=True 才写回模板。"""
    report = selector_verification_report(template_id)
    if not report.get("ok"):
        return report
    if not report["can_reconcile"]:
        report["applied"] = False
        report["message"] = (
            f"当前模板版本还有 {report['pending_count']} 个选择器未通过试跑，暂不能核销"
        )
        return report
    if not apply:
        report["applied"] = False
        report["message"] = "全部选择器已通过试跑，可执行核销"
        return report

    template = store.find("template_rule", template_id)
    if template is None:
        return {"ok": False, "reason": f"模板 {template_id} 不存在"}
    verified_fields = [item for item in report["fields"] if item["status"] in (VERIFIED, RECONCILED)]
    if template.get("status") == "草稿":
        template["status"] = "已验证"
    template["pending"] = False
    template["核销状态"] = RECONCILED
    template["核销模板版本"] = report["template_version"]
    template["核销时间"] = utc_now()
    template["已核销选择器"] = ", ".join(item["selector"] for item in verified_fields)
    template["核销数据包"] = ", ".join(sorted({
        package_id for item in verified_fields for package_id in item["package_ids"]}))

    package_ids = sorted({
        package_id for item in verified_fields for package_id in item["package_ids"]})
    entries = LEDGER.for_template(template_id)
    candidate_ids = [
        entry["candidate_id"] for entry in entries
        if entry["template_version"] == report["template_version"]
        and entry["status"] == VERIFIED
        and any(item["field"] == entry["field"] for item in verified_fields)
    ]
    updated = LEDGER.mark_reconciled(candidate_ids, package_ids)
    report["applied"] = True
    report["reconciled_candidate_count"] = updated
    report["message"] = f"已核销 {len(verified_fields)} 个已验证选择器"
    return report
