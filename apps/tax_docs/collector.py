"""采集编排：报告行 → 详情页/附件 → 纯文本 → 字段 → 暂存库。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .classify import (
    DocType,
    classify_doc_type,
    dedup_decision,
)
from .documents import TextDoc, extract_attachment_text
from .fetch import Fetcher, decode_body
from .fields import extract_fields
from .html_text import detect_attachment_urls, extract_html_text
from .recipient_table import extract_recipient_rows
from .store import now_text, set_source_status, upsert_attachment, upsert_document


def _load_json(value: str, default):
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _number_kind(number: str) -> str:
    if "不罚" in number:
        return "skip"
    if "罚告" in number or ("告" in number and "罚" in number):
        return "notice"
    if "处" in number and "罚" not in number:
        return "handling"
    if "罚" in number and "处" not in number:
        return "penalty"
    return ""


def _assign_numbers(
    numbers: list[str],
    doc_type: DocType,
    all_types: list[DocType],
    *,
    strict: bool = False,
) -> list[str]:
    """按案号中的「处/罚告/罚/不罚」标记分配文书类型。

    同一公告表格可能同时出现处理决定书、处罚决定书和不予处罚决定书；
    即使报告行只写了一种类型，也不能把「不罚」案号归到处理决定书。
    """

    matched = [number for number in numbers if _number_kind(number) == doc_type.value]
    generic = [number for number in numbers if _number_kind(number) == ""]
    if matched:
        return matched + generic
    if generic:
        return generic
    return []


def _attachment_payload(text_doc: TextDoc, filename: str) -> str:
    if not text_doc.text.strip():
        return ""
    return f"【附件 {filename} 内容】\n{text_doc.text.strip()}"


def _combine_text(page_text: str, attachment_texts: list[str]) -> str:
    parts = [page_text.strip()] if page_text.strip() else []
    parts.extend(text for text in attachment_texts if text.strip())
    return "\n\n".join(parts)


def _base_document(
    source: sqlite3.Row,
    doc_type: DocType,
    *,
    numbers: list[str],
    status: str,
    error: str = "",
    notes: str = "",
) -> dict:
    classification = classify_doc_type(doc_type)
    return {
        "record_key": f"{source['record_key']}:{doc_type.value}",
        "source_key": source["record_key"],
        "source_url": source["url"],
        "doc_type": doc_type.value,
        "category": classification.category,
        "tax_enforcement": classification.tax_enforcement,
        "law_library_hidden": classification.law_library_hidden,
        "document_type_label": classification.document_type_label,
        "dedup_by_case_number": classification.dedup_by_case_number,
        "needs_review": classification.needs_review,
        "number": numbers[0] if numbers else "",
        "numbers_json": numbers,
        "title": "",
        "agency": source["agency"] or "",
        "penalty_date": source["publish_date"] or "",
        "promulgating_date": source["publish_date"] or "",
        "party": source["party"] or "",
        "people": "",
        "party_class": "",
        "law_according": "",
        "body": "",
        "fidelity": "",
        "page_kind": "",
        "status": status,
        "error": error,
        "notes": notes or source["notes"] or "",
        "missing_json": [],
        "attachments_json": [],
        "raw_path": "",
        "fetched_at": now_text(),
    }


def _write_source_documents(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    *,
    status: str,
    error: str = "",
    notes: str = "",
) -> None:
    doc_types = [DocType(value) for value in _load_json(source["doc_types_json"], ["unknown"])]
    numbers = _load_json(source["numbers_json"], [])
    for doc_type in doc_types:
        document = _base_document(
            source, doc_type, numbers=numbers, status=status, error=error, notes=notes
        )
        upsert_document(connection, document)


def collect_source(
    connection: sqlite3.Connection,
    source: sqlite3.Row,
    fetcher: Fetcher,
    *,
    attachment_dir: str | Path,
    known_numbers: set[str] | None = None,
    refresh: bool = False,
) -> dict:
    known_numbers = known_numbers or set()
    numbers = _load_json(source["numbers_json"], [])
    doc_types = [DocType(value) for value in _load_json(source["doc_types_json"], ["unknown"])]

    if not source["url"]:
        _write_source_documents(connection, source, status="url_missing", notes="报告未提供原文链接")
        set_source_status(connection, source["record_key"], "url_missing", source["notes"])
        return {"status": "url_missing", "documents": len(doc_types)}

    if not any(doc_type not in (DocType.SKIP,) for doc_type in doc_types):
        _write_source_documents(connection, source, status="skipped", notes="不在本次三类文书范围内")
        set_source_status(connection, source["record_key"], "done", "仅包含范围外文书")
        return {"status": "skipped", "documents": len(doc_types)}

    fetch_result = fetcher.fetch(source["url"], refresh=refresh)
    if not fetch_result.ok:
        status = "blocked" if fetch_result.is_blocked else "failed"
        error = fetch_result.error or f"HTTP {fetch_result.status}"
        _write_source_documents(connection, source, status=status, error=error)
        set_source_status(connection, source["record_key"], status, error)
        return {"status": status, "documents": len(doc_types), "error": error}

    content_type = fetch_result.content_type or ""
    if "pdf" in content_type.lower() or source["url"].lower().endswith(".pdf"):
        page_text = ""
        page_meta: dict[str, str] = {}
        page_kind = "D_pdf"
        page_notes = ["详情页本身是 PDF，正文位于 PDF 中"]
    else:
        html = decode_body(fetch_result.body, content_type)
        extraction = extract_html_text(html)
        page_text = extraction.text
        page_meta = extraction.meta
        page_kind = extraction.page_kind
        page_notes = []

    attachments: list[dict] = []
    if "html" in content_type.lower() or not content_type:
        html = decode_body(fetch_result.body, content_type)
        attachments = detect_attachment_urls(html, source["url"])
    else:
        html = ""

    attachment_texts: list[str] = []
    attachment_rows: list[dict] = []
    for attachment in attachments:
        result, path = fetcher.download(attachment["url"], attachment_dir, referer=source["url"])
        filename = Path(path).name if path else attachment.get("text") or ""
        if not result.ok or path is None:
            attachment_rows.append(
                {
                    "url": attachment["url"],
                    "filename": filename,
                    "kind": "",
                    "path": "",
                    "text": "",
                    "fidelity": "",
                    "status": "blocked" if result.is_blocked else "failed",
                }
            )
            page_notes.append(f"附件下载失败：{attachment['url']} ({result.error or result.status})")
            continue
        text_doc = extract_attachment_text(path)
        payload = _attachment_payload(text_doc, filename)
        if payload:
            attachment_texts.append(payload)
        attachment_rows.append(
            {
                "url": attachment["url"],
                "filename": filename,
                "kind": text_doc.kind,
                "path": str(path),
                "text": text_doc.text,
                "fidelity": text_doc.fidelity,
                "status": "ok" if text_doc.ok else (text_doc.fidelity or "empty"),
            }
        )
        page_notes.extend(text_doc.notes)

    if not page_text and not attachment_texts and page_kind in {"D_pdf", "E_ocr"}:
        pdf_doc = TextDoc()
        if fetch_result.path:
            pdf_doc = extract_attachment_text(fetch_result.path)
        page_text = pdf_doc.text
        page_notes.extend(pdf_doc.notes)
        if pdf_doc.fidelity:
            page_kind = pdf_doc.kind or page_kind

    text = _combine_text(page_text, attachment_texts)
    if not text.strip():
        _write_source_documents(
            connection,
            source,
            status="empty_content",
            notes="；".join(page_notes) or "详情页与附件均未提取到正文",
        )
        set_source_status(connection, source["record_key"], "empty_content")
        return {"status": "empty_content", "documents": len(doc_types)}

    fidelity = ""
    if page_kind.startswith("A"):
        fidelity = "text-layer"
    elif page_kind.startswith("D"):
        fidelity = "text-layer"
    elif page_kind.startswith("E"):
        fidelity = "ocr"
    attachment_missing = bool(attachments) and not attachment_texts and len(page_text) < 300
    if attachment_rows:
        attachment_fidelity = [row["fidelity"] for row in attachment_rows if row["fidelity"]]
        if attachment_fidelity:
            fidelity = attachment_fidelity[0]
    if attachment_missing:
        fidelity = fidelity or "ocr-required"
        page_notes.append("附件正文未提取到，页面仅有公告标题")

    recipients = extract_recipient_rows(html) if html else []
    written = 0
    review_needed = False
    duplicates = 0

    def process_document(
        doc_type: DocType,
        key_suffix: str,
        party_override: str,
        numbers_pool: list[str],
    ) -> None:
        nonlocal written, review_needed, duplicates
        classification = classify_doc_type(doc_type)
        strict = bool(recipients) and len(doc_types) > 1
        assigned_numbers = _assign_numbers(
            numbers_pool, doc_type, doc_types, strict=strict
        )
        if recipients and not assigned_numbers:
            return
        document = _base_document(
            source, doc_type, numbers=assigned_numbers, status="collected"
        )
        document["record_key"] = f"{source['record_key']}:{doc_type.value}{key_suffix}"
        document.update(
            {
                "fidelity": fidelity,
                "page_kind": page_kind,
                "raw_path": fetch_result.path,
                "notes": "；".join(dict.fromkeys(page_notes)),
            }
        )

        if classification.category == 0:
            document["status"] = "skipped" if doc_type == DocType.SKIP else "needs_review"
            document["needs_review"] = 1
            upsert_document(connection, document)
            written += 1
            review_needed = review_needed or doc_type == DocType.UNKNOWN
            return

        decision = "new"
        if classification.dedup_by_case_number:
            decision = dedup_decision(assigned_numbers, known_numbers)
            if decision == "all_duplicate":
                document["status"] = "duplicate"
                document["notes"] = (
                    document["notes"] + "；" if document["notes"] else ""
                ) + "案号已存在"
                duplicates += 1
                upsert_document(connection, document)
                written += 1
                return

        fields = extract_fields(
            text,
            page_meta,
            source_party=party_override or source["party"],
            fallback_numbers=assigned_numbers,
        )
        if attachment_missing:
            fields.body = ""
            if "body" not in fields.missing:
                fields.missing.append("body")
        title = fields.title
        if recipients and len(recipients) > 1 and party_override and party_override not in title:
            title = f"{title}-{party_override}" if title else party_override
        if not assigned_numbers:
            assigned_numbers = [
                number
                for number in fields.numbers
                if _number_kind(number) in (doc_type.value, "")
            ]
        document.update(
            {
                "title": title,
                "number": assigned_numbers[0] if assigned_numbers else (fields.number if not fields.numbers else ""),
                "numbers_json": assigned_numbers,
                "agency": fields.agency or document["agency"],
                "penalty_date": fields.penalty_date or document["penalty_date"],
                "promulgating_date": fields.promulgating_date or document["promulgating_date"],
                "party": party_override or fields.party or document["party"],
                "people": fields.people,
                "party_class": fields.party_class,
                "law_according": fields.law_according,
                "body": fields.body or text,
                "missing_json": fields.missing,
            }
        )
        critical_missing = any(
            field in fields.missing
            for field in ("adminPenaltyTitle", "adminPenaltyNumber", "trialCourtNameOld", "body")
        )
        if decision == "partial_duplicate":
            document["status"] = "partial_duplicate"
            document["notes"] = (
                document["notes"] + "；" if document["notes"] else ""
            ) + "部分案号已存在"
        elif decision == "no_number" and classification.dedup_by_case_number:
            document["status"] = "needs_review"
            document["notes"] = (
                document["notes"] + "；" if document["notes"] else ""
            ) + "处罚决定书缺少案号"
            critical_missing = True
        if fidelity in {"ocr-required", "unsupported"}:
            document["status"] = "needs_review"
            critical_missing = True
        if critical_missing:
            document["needs_review"] = 1
            review_needed = True
        document["needs_review"] = int(document["needs_review"] or critical_missing)
        upsert_document(connection, document)
        for attachment in attachment_rows:
            upsert_attachment(
                connection,
                record_key=document["record_key"],
                url=attachment["url"],
                filename=attachment["filename"],
                kind=attachment["kind"],
                path=attachment["path"],
                text=attachment["text"],
                fidelity=attachment["fidelity"],
                status=attachment["status"],
            )
        document["attachments_json"] = attachment_rows
        upsert_document(connection, document)
        written += 1
        if classification.dedup_by_case_number and decision != "partial_duplicate":
            known_numbers.update(assigned_numbers)

    if recipients:
        for doc_type in doc_types:
            for recipient in recipients:
                process_document(
                    doc_type,
                    f":r{recipient['row_no']}",
                    recipient.get("party", ""),
                    recipient.get("numbers", []),
                )
    else:
        for doc_type in doc_types:
            process_document(doc_type, "", "", numbers)

    source_status = "review" if review_needed else "done"
    if written == 0:
        source_status = "review"
        set_source_status(
            connection,
            source["record_key"],
            source_status,
            "未匹配到对应文书类型的案号，请人工核对",
        )
    elif duplicates and not written - duplicates:
        source_status = "done"
        set_source_status(connection, source["record_key"], source_status)
    else:
        set_source_status(connection, source["record_key"], source_status)
    return {
        "status": source_status,
        "documents": written,
        "duplicates": duplicates,
        "fidelity": fidelity,
        "attachments": len(attachment_rows),
    }


def collect_pending(
    connection: sqlite3.Connection,
    fetcher: Fetcher,
    *,
    attachment_dir: str | Path,
    limit: int = 0,
    province: str = "",
    known_numbers: set[str] | None = None,
    refresh: bool = False,
    progress: bool = True,
) -> dict[str, int]:
    sources = [
        row
        for row in connection.execute("SELECT * FROM sources WHERE status='pending' ORDER BY report, row_no")
    ]
    if province:
        sources = [row for row in sources if province in row["province"]]
    if limit:
        sources = sources[:limit]

    summary: dict[str, int] = {"total": len(sources), "done": 0, "blocked": 0, "failed": 0, "review": 0}
    for index, source in enumerate(sources, 1):
        try:
            result = collect_source(
                connection,
                source,
                fetcher,
                attachment_dir=attachment_dir,
                known_numbers=known_numbers,
                refresh=refresh,
            )
        except Exception as exc:  # 保证单条失败不影响整批
            _write_source_documents(connection, source, status="failed", error=str(exc))
            set_source_status(connection, source["record_key"], "failed", str(exc))
            result = {"status": "failed", "error": str(exc)}
        status = result.get("status", "failed")
        if status in ("done", "skipped"):
            summary["done"] += 1
        elif status in ("blocked", "url_missing"):
            summary["blocked"] += 1
        elif status == "review":
            summary["review"] += 1
        else:
            summary["failed"] += 1
        if progress:
            print(
                f"[{index}/{len(sources)}] {status:14s} {source['province']} "
                f"{source['publish_date']} {source['url'][:90]}"
            )
    return summary
