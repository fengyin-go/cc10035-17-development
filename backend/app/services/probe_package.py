"""探针试跑配置数据包的编解码、校验和原子存储。

数据包是模板规则模块交付给采集端的离线文件，采集端不需要访问本服务即可
按样本页复现模板推荐的字段选择器。包内同时保留一份生成时的模板快照，
因此模板后续被修改后，旧包仍然能被还原和试跑。

包结构（ZIP）：:

    manifest.json    本轮交付声明：模板快照、可试跑/待验证候选、冲突额度、文件校验和
    template.json    生成时刻的模板快照（字段、选择器、样本页）
    candidates.json  字段候选清单（含探针配置与状态）
    probes.json      旧版探针清单（按 探针名称/目标页面/选择器列表 组织，便于历史消费方读取）
    README.txt       采集端使用说明
    SHA256SUMS       除 manifest.json 外每个文件的 SHA-256

兼容性：解码器同时接受 1.x 版本的 ZIP 包和早期直接交付的 JSON 文档；
字段名通过别名表兼容历史中文含义（如 选择器列表 → selector）。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

FORMAT_NAME = "probe-trial-package"
CURRENT_SCHEMA_VERSION = "1.1"
LEGACY_SCHEMA_VERSION = "1.0"
SUPPORTED_MAJOR_VERSIONS = {"1"}
MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "SHA256SUMS"
TEMPLATE_NAME = "template.json"
CANDIDATES_NAME = "candidates.json"
PROBES_NAME = "probes.json"
README_NAME = "README.txt"
PACKAGE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SELECTOR_START_RE = re.compile(r"^(?:[.#\[]|[A-Za-z_*@/])[^\r\n]*$")

# 字段语义表会随包一起交付，历史包没有这张表时按这里的定义解释。
FIELD_SEMANTICS: dict[str, str] = {
    "candidate_id": "字段候选的稳定标识，同一模板同一选择器跨包保持一致",
    "template_id": "候选来源模板 ID",
    "template_name": "候选来源模板名称",
    "template_version": "生成数据包时的模板版本摘要，用于事后还原当时模板",
    "site": "模板适用站点",
    "field": "模板声明的提取字段名",
    "selector": "该字段推荐的 CSS/XPath 选择器",
    "sample_page": "复现该选择器应使用的样本页",
    "status": "本轮状态：可试跑 / 待验证",
    "reason": "不可试跑或待验证的原因",
    "probe": "可直接交给采集端执行的探针配置",
    "quota_holder": "该候选是否占用本轮页面采样额度",
    "conflicts_with": "与本候选选择器冲突、共享同一采样额度的候选",
}

# 历史字段别名：读包时按顺序取第一个非空值，写包时同时写出标准名与历史名。
CANDIDATE_ALIASES: dict[str, tuple[str, ...]] = {
    "candidate_id": ("candidate_id", "候选ID", "字段候选ID"),
    "field": ("field", "字段", "字段名称", "提取字段"),
    "selector": ("selector", "选择器", "选择器列表", "探针选择器"),
    "sample_page": ("sample_page", "样本页", "采样页面", "目标页面", "页面URI"),
    "template_id": ("template_id", "模板ID", "来源模板ID"),
    "template_name": ("template_name", "模板名称", "来源模板名称"),
    "site": ("site", "适用站点", "站点"),
    "status": ("status", "状态"),
    "reason": ("reason", "原因", "说明"),
}
TEMPLATE_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "template_id", "模板ID"),
    "name": ("name", "template_name", "模板名称"),
    "site": ("site", "适用站点", "站点"),
    "fields": ("fields", "提取字段"),
    "selectors": ("selectors", "字段选择器"),
    "sample_pages": ("sample_pages", "sample_page", "采样页面", "样本页"),
    "status": ("status", "状态"),
    "version": ("version", "template_version", "模板版本"),
}


class PackageValidationError(ValueError):
    """数据包无法通过完整性或格式校验。"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_package_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"pkg-{stamp}-{uuid.uuid4().hex[:8]}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def split_multi(value: Any) -> list[str]:
    """按逗号、分号或换行拆分历史遗留的“选择器列表/采样页面”字符串。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,，;；\n]+", str(value))
    result: list[str] = []
    for item in raw:
        text = item.strip()
        if text and text not in result:
            result.append(text)
    return result


def first_value(source: dict[str, Any], keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return default


def is_selector_syntax_plausible(selector: Any) -> bool:
    """判断选择器是否像一个可执行的选择器，避免把空值或说明文字发给采集端。"""
    if selector is None:
        return False
    text = str(selector).strip()
    if not text or len(text) > 512:
        return False
    if any(ord(char) < 32 for char in text):
        return False
    if text.count("[") != text.count("]") or text.count("(") != text.count(")"):
        return False
    if text.count('"') % 2 or text.count("'") % 2:
        return False
    if not _SELECTOR_START_RE.match(text):
        return False
    # 空白分隔的说明文字（例如“标题 和 作者”）不是合法选择器：每个片段都要以
    # 选择器字符开头，中文或说明性文字开头的一律判为不可执行。
    for token in re.split(r"\s+", text):
        if token and not re.match(r"^(?:[.#\[:*>+~/]|[A-Za-z_])", token):
            return False
    return True


def candidate_id_for(template_id: Any, field: Any, selector: Any, sample_page: Any) -> str:
    try:
        tid = int(template_id)
    except (TypeError, ValueError):
        tid = 0
    digest = sha256_text(f"{field}|{selector}|{sample_page}")[:10]
    return f"cand-{tid}-{digest}"


def _canonical_template(doc: dict[str, Any] | None) -> dict[str, Any]:
    doc = doc or {}
    fields = split_multi(first_value(doc, TEMPLATE_ALIASES["fields"], []))
    selectors = split_multi(first_value(doc, TEMPLATE_ALIASES["selectors"], []))
    pages = split_multi(first_value(doc, TEMPLATE_ALIASES["sample_pages"], []))
    return {
        "id": first_value(doc, TEMPLATE_ALIASES["id"], 0),
        "name": str(first_value(doc, TEMPLATE_ALIASES["name"], "")),
        "site": str(first_value(doc, TEMPLATE_ALIASES["site"], "")),
        "status": str(first_value(doc, TEMPLATE_ALIASES["status"], "")),
        "fields": fields,
        "selectors": selectors,
        "sample_pages": pages,
        "version": str(first_value(doc, TEMPLATE_ALIASES["version"], "")),
    }


def normalize_template(doc: dict[str, Any] | None, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    template = _canonical_template(doc)
    if manifest:
        fallback = _canonical_template(manifest.get("template") if isinstance(manifest.get("template"), dict) else {})
        for key, value in fallback.items():
            if not template.get(key) and value:
                template[key] = value
    template["id"] = int(template["id"]) if str(template["id"]).isdigit() else template["id"]
    return template


def normalize_templates(
    doc: Any,
    manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """读取包内模板快照；单模板和“同一站点两份模板”两种写法都支持。"""
    docs: list[Any] = []
    if isinstance(doc, list):
        docs = doc
    elif isinstance(doc, dict):
        nested = doc.get("templates")
        if isinstance(nested, list):
            docs = nested
        elif doc:
            docs = [doc]
    manifest_templates: list[Any] = []
    if manifest:
        value = manifest.get("templates")
        if isinstance(value, list):
            manifest_templates = value
        elif isinstance(manifest.get("template"), dict):
            manifest_templates = [manifest["template"]]

    templates: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for item in docs:
        if not isinstance(item, dict):
            continue
        template = normalize_template(item)
        if template["id"] in seen:
            continue
        seen.add(template["id"])
        templates.append(template)
    # 包内缺模板快照时，用 manifest 里的声明补齐。
    for item in manifest_templates:
        if not isinstance(item, dict):
            continue
        template = normalize_template(item)
        if template["id"] in seen or not template["selectors"]:
            continue
        seen.add(template["id"])
        templates.append(template)
    if not templates and manifest_templates:
        templates = [normalize_template(item) for item in manifest_templates if isinstance(item, dict)]
    return templates


def _status_is_pending(value: str) -> bool:
    return str(value or "").strip() in {"待验证", "pending", "deferred", "未试跑"}


def _template_for(
    entry: dict[str, Any],
    template_id: Any,
    templates: list[dict[str, Any]],
    default: dict[str, Any],
) -> dict[str, Any]:
    for template in templates:
        if str(template.get("id")) == str(template_id):
            return template
    return default


def normalize_candidates(
    doc: Any,
    template: dict[str, Any] | list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """把新旧两种候选/探针写法归一成同一份字段候选清单。

    候选可以自带 template_id 指到包内任意一份模板快照；没有时回落到单模板。
    """
    templates = template if isinstance(template, list) else [template]
    default_template = templates[0] if templates else _canonical_template({})

    entries: list[Any] = []
    if isinstance(doc, list):
        entries = doc
    elif isinstance(doc, dict):
        for key in ("candidates", "probes", "候选", "探针列表", "field_candidates"):
            value = doc.get(key)
            if isinstance(value, list):
                entries = value
                break
        if not entries and doc.get("selectors") is not None:
            entries = doc.get("selectors") or []

    candidates: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        template_id = first_value(entry, CANDIDATE_ALIASES["template_id"], "")
        if not template_id:
            template_id = default_template.get("id", 0)
        scoped = _template_for(entry, template_id, templates, default_template)
        field = str(first_value(entry, CANDIDATE_ALIASES["field"], "") or "")
        selector = str(first_value(entry, CANDIDATE_ALIASES["selector"], "") or "")
        sample_page = str(first_value(entry, CANDIDATE_ALIASES["sample_page"], "") or "")
        if not sample_page and scoped.get("sample_pages"):
            sample_page = scoped["sample_pages"][0]
        if not field and index < len(scoped.get("fields", [])):
            field = scoped["fields"][index]
        if not selector and index < len(scoped.get("selectors", [])):
            selector = scoped["selectors"][index]
        raw_status = str(first_value(entry, CANDIDATE_ALIASES["status"], "") or "")
        legacy = not raw_status
        status = "待验证" if _status_is_pending(raw_status) else "可试跑"
        reason = str(first_value(entry, CANDIDATE_ALIASES["reason"], "") or "")
        candidate_id = str(
            first_value(entry, CANDIDATE_ALIASES["candidate_id"], "")
            or candidate_id_for(template_id, field, selector, sample_page)
        )
        probe = entry.get("probe") if isinstance(entry.get("probe"), dict) else {
            "探针名称": entry.get("探针名称") or f"{scoped.get('name') or '模板'}-{field or index + 1}",
            "目标页面": sample_page,
            "选择器列表": selector,
            "验证关键字": entry.get("验证关键字", ""),
            "采样频率": entry.get("采样频率", "daily"),
            "超时秒数": entry.get("超时秒数", "30"),
            "重试次数": entry.get("重试次数", "3"),
        }
        candidate_template_id = int(template_id) if str(template_id).isdigit() else scoped.get("id", 0)
        candidates.append({
            "candidate_id": candidate_id,
            "template_id": candidate_template_id,
            "template_name": str(
                first_value(entry, CANDIDATE_ALIASES["template_name"], "")
                or scoped.get("name", "")
            ),
            "template_version": str(entry.get("template_version") or scoped.get("version", "")),
            "site": str(first_value(entry, CANDIDATE_ALIASES["site"], "") or scoped.get("site", "")),
            "field": field,
            "selector": selector,
            "sample_page": sample_page,
            "status": status,
            "reason": reason,
            "quota_holder": bool(entry.get("quota_holder", status == "可试跑")),
            "conflicts_with": list(entry.get("conflicts_with") or []),
            "probe": probe,
            "legacy": legacy,
            "legacy_fields": {
                "字段": field,
                "选择器列表": selector,
                "目标页面": sample_page,
            },
        })

    # 包内没有候选时，从每份模板快照补齐候选：缺选择器的字段标记为待验证。
    covered = {(c["template_id"], c["field"], c["selector"], c["sample_page"]) for c in candidates}
    for scoped in templates:
        if not scoped.get("selectors") and not scoped.get("fields"):
            continue
        fields = list(scoped.get("fields") or [])
        selectors = list(scoped.get("selectors") or [])
        for index in range(max(len(fields), len(selectors))):
            field = fields[index] if index < len(fields) else f"字段{index + 1}"
            selector = selectors[index] if index < len(selectors) else ""
            page = scoped["sample_pages"][0] if scoped.get("sample_pages") else ""
            key = (scoped.get("id"), field, selector, page)
            if key in covered:
                continue
            covered.add(key)
            if not selector:
                status, reason = "待验证", "模板缺少该字段的选择器"
            elif not page:
                status, reason = "待验证", "模板没有给出可复现的样本页"
            elif not is_selector_syntax_plausible(selector):
                status, reason = "待验证", "选择器语法不合法，需要先修正模板"
            else:
                status, reason = "可试跑", ""
            candidates.append({
                "candidate_id": candidate_id_for(scoped["id"], field, selector, page),
                "template_id": scoped["id"],
                "template_name": scoped.get("name", ""),
                "template_version": scoped.get("version", ""),
                "site": scoped.get("site", ""),
                "field": field,
                "selector": selector,
                "sample_page": page,
                "status": status,
                "reason": reason,
                "quota_holder": status == "可试跑",
                "conflicts_with": [],
                "probe": {
                    "探针名称": f"{scoped.get('name') or '模板'}-{field}",
                    "目标页面": page,
                    "选择器列表": selector,
                    "验证关键字": "",
                    "采样频率": "daily",
                    "超时秒数": "30",
                    "重试次数": "3",
                },
                "legacy": True,
                "legacy_fields": {"字段": field, "选择器列表": selector, "目标页面": page},
            })
    return candidates


def encode_package(
    *,
    manifest: dict[str, Any],
    template_doc: dict[str, Any] | list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    probes: list[dict[str, Any]],
    readme: str,
) -> bytes:
    """在内存中完整编码一个 ZIP 包，编码成功后才交给原子存储。"""
    if isinstance(template_doc, list):
        template_payload: Any = {"templates": template_doc}
    else:
        template_payload = template_doc
    files = {
        TEMPLATE_NAME: json_dumps(template_payload),
        CANDIDATES_NAME: json_dumps({"candidates": candidates}),
        PROBES_NAME: json_dumps({"probes": probes}),
        README_NAME: readme,
    }
    checksums = {name: sha256_text(text) for name, text in files.items()}
    # 就地补齐 manifest，让调用方拿到的交付声明与包内文件完全一致。
    manifest["format"] = FORMAT_NAME
    manifest["schema_version"] = str(manifest.get("schema_version") or CURRENT_SCHEMA_VERSION)
    manifest["files"] = [
        {"path": name, "sha256": checksums[name], "size": len(text.encode("utf-8"))}
        for name, text in sorted(files.items())
    ]
    manifest["checksum_file"] = CHECKSUM_NAME
    manifest_text = json_dumps(manifest)
    checksum_text = "".join(
        f"{checksums[name]}  {name}\n" for name in sorted(files)
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in [MANIFEST_NAME, *sorted(files), CHECKSUM_NAME]:
            text = manifest_text if name == MANIFEST_NAME else (
                checksum_text if name == CHECKSUM_NAME else files[name])
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, text.encode("utf-8"))
    data = buffer.getvalue()

    # 编码完成后先自检，避免把 CRC 损坏的字节交给下载端。
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if archive.testzip() is not None:
            raise PackageValidationError("数据包编码自检失败，请重试")
    return data


def _verify_checksum_manifest(archive: zipfile.ZipFile, names: set[str], manifest: dict[str, Any]) -> bool:
    """校验 manifest.files 声明的每个文件；返回是否真的做了校验。"""
    declared = manifest.get("files")
    if not isinstance(declared, list) or not declared:
        return False
    for item in declared:
        if not isinstance(item, dict):
            raise PackageValidationError("数据包 manifest.files 结构不合法")
        path = str(item.get("path") or "")
        expected = str(item.get("sha256") or "")
        if not path or not expected:
            raise PackageValidationError("数据包 manifest.files 缺少文件路径或校验和")
        if path not in names:
            raise PackageValidationError(f"数据包缺少声明文件：{path}")
        actual = sha256_bytes(archive.read(path))
        if actual != expected:
            raise PackageValidationError(f"文件校验和不匹配：{path}")
    return True


def _verify_sha256sums(archive: zipfile.ZipFile, names: set[str]) -> bool:
    if CHECKSUM_NAME not in names:
        return False
    text = archive.read(CHECKSUM_NAME).decode("utf-8")
    checked = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise PackageValidationError("SHA256SUMS 行格式不正确")
        expected, path = parts[0], parts[1].lstrip("*").strip()
        if path not in names:
            raise PackageValidationError(f"数据包缺少校验清单中的文件：{path}")
        actual = sha256_bytes(archive.read(path))
        if actual != expected:
            raise PackageValidationError(f"文件校验和不匹配：{path}")
        checked += 1
    if checked == 0:
        raise PackageValidationError("SHA256SUMS 为空，无法校验数据包")
    return True


def _decode_json_document(data: bytes) -> dict[str, Any]:
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageValidationError("数据包不是有效的 UTF-8 JSON 文档") from exc
    if not isinstance(document, dict):
        raise PackageValidationError("数据包 JSON 顶层必须是对象")
    manifest = document.get("manifest") if isinstance(document.get("manifest"), dict) else {}
    template_doc: Any = document.get("template")
    if template_doc is None and document.get("templates") is not None:
        template_doc = document.get("templates")
    if template_doc is None and document.get("selectors") is not None:
        template_doc = document
    templates = normalize_templates(template_doc, manifest)
    if not templates:
        templates = [normalize_template(None)]
    candidates = normalize_candidates(document.get("candidates") or document.get("probes") or [],
                                      templates, manifest)
    payload_manifest = {
        "format": manifest.get("format", FORMAT_NAME),
        "schema_version": str(manifest.get("schema_version") or LEGACY_SCHEMA_VERSION),
        "package_id": manifest.get("package_id") or f"json-{sha256_bytes(data)[:12]}",
        "round_id": manifest.get("round_id") or "",
        "declaration": manifest.get("declaration") or {},
        "conflicts": manifest.get("conflicts") or [],
        "templates": templates,
    }
    return {
        "manifest": payload_manifest,
        "template": templates[0],
        "templates": templates,
        "candidates": candidates,
        "package_sha256": sha256_bytes(data),
        "checksums_verified": False,
        "legacy_document": True,
        "warnings": ["旧版 JSON 数据包未附带文件校验清单，已按兼容模式读取"],
    }


def decode_package(data: bytes) -> dict[str, Any]:
    """解析并校验数据包；任何不完整或校验失败都会抛 PackageValidationError。"""
    if not data:
        raise PackageValidationError("数据包为空")
    if data.lstrip()[:1] == b"{":
        return _decode_json_document(data)

    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise PackageValidationError("数据包不是完整的 ZIP 文件，可能传输或打包中断") from exc

    with archive:
        corrupt = archive.testzip()
        if corrupt is not None:
            raise PackageValidationError(f"数据包条目 CRC 校验失败：{corrupt}")
        names = set(archive.namelist())
        if MANIFEST_NAME not in names:
            raise PackageValidationError("数据包缺少 manifest.json")
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PackageValidationError("manifest.json 不是有效的 UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise PackageValidationError("manifest.json 顶层必须是对象")

        file_format = str(manifest.get("format") or "")
        if file_format and file_format != FORMAT_NAME:
            raise PackageValidationError(f"数据包格式不受支持：{file_format}")
        version = str(manifest.get("schema_version") or LEGACY_SCHEMA_VERSION)
        if version.split(".")[0] not in SUPPORTED_MAJOR_VERSIONS:
            raise PackageValidationError(f"数据包版本不受支持：{version}")

        warnings: list[str] = []
        verified = False
        if _verify_sha256sums(archive, names):
            verified = True
        if _verify_checksum_manifest(archive, names, manifest):
            verified = True
        if not verified:
            warnings.append("数据包未附带校验清单，按历史兼容模式读取")

        def read_json(name: str) -> Any:
            if name not in names:
                return None
            try:
                return json.loads(archive.read(name).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PackageValidationError(f"{name} 不是有效的 UTF-8 JSON") from exc

        template_doc = read_json(TEMPLATE_NAME)
        if template_doc is None:
            template_doc = manifest.get("templates") or manifest.get("template")
        candidates_doc = read_json(CANDIDATES_NAME)
        if candidates_doc is None:
            candidates_doc = manifest.get("candidates")
        if candidates_doc is None:
            candidates_doc = read_json(PROBES_NAME)
        if candidates_doc is None:
            candidates_doc = manifest.get("probes") or []

    templates = normalize_templates(template_doc, manifest)
    if not templates:
        raise PackageValidationError("数据包缺少模板快照，无法还原当时的模板和选择器")
    candidates = normalize_candidates(candidates_doc, templates, manifest)
    if not candidates:
        raise PackageValidationError("数据包没有可还原的字段候选")

    return {
        "manifest": manifest,
        "template": templates[0],
        "templates": templates,
        "candidates": candidates,
        "package_sha256": sha256_bytes(data),
        "checksums_verified": verified,
        "legacy_document": False,
        "warnings": warnings,
    }


class PackageStore:
    """数据包的原子文件存储。

    写入流程：临时文件 → fsync → os.replace 到正式路径 → 原子写校验和旁车文件。
    读取时同时要求正式文件和旁车文件存在且 SHA-256 与大小一致；临时文件永远不会
    被读取，因此打包中断后重试不会让接收方拿到半成品。
    """

    def __init__(self, root: str | Path | None = None) -> None:
        configured = root or os.environ.get("PROBE_PACKAGE_DIR")
        if not configured:
            configured = Path(tempfile.gettempdir()) / "web-semantic-probe-packages"
        self.root = Path(configured)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _paths(self, package_id: str) -> tuple[Path, Path]:
        if not PACKAGE_ID_RE.match(package_id or ""):
            raise PackageValidationError(f"数据包标识不合法：{package_id}")
        return self.root / f"{package_id}.zip", self.root / f"{package_id}.json"

    def _atomic_write(self, path: Path, data: bytes) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    pass

    def save(self, package_id: str, data: bytes) -> dict[str, Any]:
        zip_path, meta_path = self._paths(package_id)
        digest = sha256_bytes(data)
        meta = {
            "package_id": package_id,
            "sha256": digest,
            "size": len(data),
            "saved_at": utc_now(),
            "complete": True,
        }
        payload = json_dumps(meta).encode("utf-8")
        with self._lock:
            self._atomic_write(zip_path, data)
            self._atomic_write(meta_path, payload)
        return meta

    def load(self, package_id: str) -> bytes | None:
        try:
            zip_path, meta_path = self._paths(package_id)
        except PackageValidationError:
            return None
        if not zip_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            data = zip_path.read_bytes()
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(meta, dict) or not meta.get("complete"):
            return None
        if meta.get("size") != len(data) or meta.get("sha256") != sha256_bytes(data):
            # 半成品或损坏文件：视为不存在，调用方会用同一标识重新打包。
            return None
        return data

    def meta(self, package_id: str) -> dict[str, Any] | None:
        try:
            _, meta_path = self._paths(package_id)
        except PackageValidationError:
            return None
        if not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return meta if isinstance(meta, dict) else None

    def discard(self, package_id: str) -> None:
        try:
            zip_path, meta_path = self._paths(package_id)
        except PackageValidationError:
            return
        for path in (zip_path, meta_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass

    def list_packages(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for meta_path in sorted(self.root.glob("*.json")):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(meta, dict) and meta.get("package_id"):
                items.append(meta)
        return items


PACKAGE_STORE = PackageStore()


def build_and_save(
    package_id: str,
    builder: Callable[[], bytes],
    *,
    store: PackageStore | None = None,
    attempts: int = 2,
) -> tuple[bytes, dict[str, Any]]:
    """打包 + 落盘，失败时清理临时文件并可重试同一标识。"""
    target = store or PACKAGE_STORE
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            data = builder()
            if not data:
                raise PackageValidationError("打包结果为空")
            meta = target.save(package_id, data)
            verified = target.load(package_id)
            if verified != data:
                raise PackageValidationError("数据包落盘校验失败")
            return data, meta
        except Exception as exc:  # noqa: BLE001 - 打包链路需要把失败原因统一抛出
            last_error = exc
            target.discard(package_id)
    raise PackageValidationError(f"数据包打包失败，请重试：{last_error}") from last_error


def package_bytes_from_payload(payload: Any) -> tuple[bytes | None, str | None]:
    """从 JSON 请求体里取包内容（base64 或 package_id）。"""
    if not isinstance(payload, dict):
        return None, None
    encoded = payload.get("package_base64") or payload.get("content_base64")
    if encoded:
        try:
            return base64.b64decode(str(encoded), validate=True), None
        except (ValueError, TypeError) as exc:
            raise PackageValidationError("package_base64 不是合法的 Base64 内容") from exc
    package_id = payload.get("package_id")
    return None, str(package_id) if package_id else None
