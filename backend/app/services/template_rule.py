"""模板规则领域服务：管理站点级字段抽取模板。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class TemplateRuleService(BaseModuleService):
    MODULE = "template_rule"
    ENTITY = "规则模板"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["模板名称", "适用站点", "提取字段", "字段选择器", "采样页面", "校验规则", "更新策略"]
    REQUIRED_FIELDS = ["模板名称", "适用站点", "提取字段"]
    STATUS_ORDER = ["草稿", "已验证", "已发布", "已失效"]
    ACTION_RULES = {"验证模板": "草稿", "发布模板": "已验证", "标记失效": "已发布"}
    NEGATIVE_ACTIONS = []

    def suggest_probes(self, template_id: int) -> dict[str, Any]:
        """根据模板规则推导建议的探针配置，供探针选择模块消费。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "reason": "模板不存在"}
        site = template.get("适用站点", "")
        selectors_str = template.get("字段选择器", "")
        sample_page = template.get("采样页面", "")
        selectors = [s.strip() for s in selectors_str.split(",") if s.strip()]

        probes = []
        for i, sel in enumerate(selectors):
            page = sample_page if sample_page else site
            probes.append({
                "探针名称": f"{template.get('模板名称')}-probe-{i+1}",
                "目标页面": page,
                "选择器列表": sel,
                "验证关键字": "",
                "采样频率": "daily",
                "超时秒数": "30",
                "重试次数": "3",
            })
        return {
            "ok": True,
            "template_id": template_id,
            "template_name": template.get("模板名称"),
            "suggested_probe_count": len(probes),
            "probes": probes,
        }

    def compute_template_coverage(self) -> dict[str, Any]:
        """计算模板覆盖范围，供抽取指标消费。"""
        rows = store.rows(self.MODULE)
        sites: dict[str, int] = {}
        total_selectors = 0
        total_fields = 0
        for row in rows:
            site = row.get("适用站点", "")
            sites[site] = sites.get(site, 0) + 1
            total_selectors += len(row.get("字段选择器", "").split(",")) if row.get("字段选择器", "") else 0
            total_fields += len(row.get("提取字段", "").split(",")) if row.get("提取字段", "") else 0
        return {
            "template_count": len(rows),
            "site_count": len(sites),
            "total_selectors": total_selectors,
            "total_target_fields": total_fields,
            "published": sum(1 for r in rows if r.get("status") == "已发布"),
        }

    def get_published_templates(self) -> list[dict[str, Any]]:
        """取出所有已发布模板，供质量检查跨模块比对。"""
        return [
            {
                "id": r.get("id"),
                "name": r.get("模板名称"),
                "site": r.get("适用站点"),
                "fields": r.get("提取字段"),
                "selectors": r.get("字段选择器"),
                "sample_page": r.get("采样页面"),
            }
            for r in store.rows(self.MODULE)
            if r.get("status") == "已发布"
        ]

    def build_trial_package(
        self,
        template_ids: list[int] | None = None,
        *,
        quota_per_page: int = 1,
        all_templates: bool = False,
    ) -> dict[str, Any]:
        """把模板的探针候选导出为采集端可试跑的数据包，供探针试跑模块交付。"""
        from app.services.probe_package import PackageValidationError
        from app.services.probe_package_builder import build_package, package_summary

        ids = list(template_ids or [])
        try:
            package = build_package(
                template_ids=ids,
                all_templates=all_templates,
                quota_per_page=quota_per_page,
            )
        except PackageValidationError as exc:
            return {"ok": False, "reason": str(exc)}
        return package_summary(package)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["template_coverage"] = self.compute_template_coverage()
        return base