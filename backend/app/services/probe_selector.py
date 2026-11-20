"""探针选择领域服务：管理页面元素的自动化探测探针。"""
from __future__ import annotations

import hashlib
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ProbeSelectorService(BaseModuleService):
    MODULE = "probe_selector"
    ENTITY = "探针配置"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["探针名称", "目标页面", "选择器列表", "验证关键字", "采样频率", "超时秒数", "重试次数"]
    REQUIRED_FIELDS = ["探针名称", "目标页面", "选择器列表"]
    STATUS_ORDER = ["草稿", "探测中", "已验证", "已废弃"]
    ACTION_RULES = {"启动探测": "草稿", "验证探针": "探测中", "废弃探针": "已验证"}
    NEGATIVE_ACTIONS = []

    def trigger_snapshot(self, probe_id: int) -> dict[str, Any]:
        """触发页面快照：探针→快照的跨模块联动。被页面快照消费。"""
        probe = store.find(self.MODULE, probe_id)
        if probe is None:
            return {"ok": False, "reason": "探针不存在"}
        target_page = probe.get("目标页面", "")
        selectors = probe.get("选择器列表", "")

        page_hash = hashlib.md5(target_page.encode()).hexdigest()[:8]
        dom_hash = hashlib.sha256(
            f"{target_page}|{selectors}|{hashlib.md5(b'dummy_dom').hexdigest()[:8]}".encode()
        ).hexdigest()[:16]
        text_hash = hashlib.sha256(
            f"{target_page}|text_content".encode()
        ).hexdigest()[:16]

        return {
            "ok": True,
            "probe_id": probe_id,
            "probe_name": probe.get("探针名称"),
            "target_page": target_page,
            "snapshot_request": {
                "快照ID": f"snap-{page_hash}",
                "页面URI": target_page,
                "DOM哈希": dom_hash,
                "文本哈希": text_hash,
                "抓取时间": "2026-11-19T10:00:00Z",
                "页面大小": "45.2KB",
            },
        }

    def compute_probe_health(self) -> dict[str, Any]:
        """探针健康度计算，供聚合视图和抽取指标消费。"""
        rows = store.rows(self.MODULE)
        valid = sum(1 for r in rows if r.get("status") == "已验证")
        active = sum(1 for r in rows if r.get("status") == "探测中")
        dead = sum(1 for r in rows if r.get("status") == "已废弃")
        total = len(rows)
        return {
            "total_probes": total,
            "valid_probes": valid,
            "active_probes": active,
            "dead_probes": dead,
            "health_ratio": round(valid / total, 2) if total > 0 else 0.0,
        }

    def get_probes_for_page(self, page_uri: str) -> list[dict[str, Any]]:
        """按目标页面过滤探针。"""
        return [
            {"id": r.get("id"), "name": r.get("探针名称"),
             "selectors": r.get("选择器列表"), "status": r.get("status")}
            for r in store.rows(self.MODULE)
            if r.get("目标页面") == page_uri
        ]

    def consume_trial_package(self, package_id: str) -> dict[str, Any]:
        """读取探针试跑配置数据包，按样本页触发快照并回写字段候选。被模板规则消费。"""
        from app.services.probe_package import PACKAGE_STORE, PackageValidationError, decode_package
        from app.services.probe_trial import run_trial

        data = PACKAGE_STORE.load(package_id)
        if data is None:
            return {"ok": False, "reason": f"数据包 {package_id} 不存在或未完整落盘"}
        try:
            decoded = decode_package(data)
        except PackageValidationError as exc:
            return {"ok": False, "reason": str(exc)}
        record = run_trial(decoded, source="probe_selector")
        return {
            "ok": True,
            "package_id": package_id,
            "trial_run_id": record["run_id"],
            "snapshot_ids": [item["snapshot_code"] for item in record["snapshots"]],
            "verified_count": record["writeback"]["verified_count"],
            "pending_count": record["writeback"]["pending_count"],
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["probe_health"] = self.compute_probe_health()
        from app.services.probe_trial import LEDGER
        base["trial_link"] = {
            "field_candidates": len(LEDGER.list()),
            "verified": len(LEDGER.list(status="已验证")),
            "pending": len(LEDGER.list(status="待验证")),
        }
        return base