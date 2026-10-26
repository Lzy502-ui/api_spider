"""把暂存库导出为可复核、可入库的 JSONL / CSV / SQL。"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from .classify import CATEGORY_DEDUP, CATEGORY_EXTRA_TABLES, CATEGORY_NORMAL
from .store import source_url_categories


FIELDS = (
    "record_key",
    "source_key",
    "source_url",
    "doc_type",
    "category",
    "tax_enforcement",
    "law_library_hidden",
    "document_type_label",
    "dedup_by_case_number",
    "needs_review",
    "title",
    "number",
    "numbers_json",
    "agency",
    "penalty_date",
    "promulgating_date",
    "party",
    "people",
    "party_class",
    "law_according",
    "body",
    "fidelity",
    "page_kind",
    "status",
    "error",
    "notes",
    "missing_json",
    "attachments_json",
    "raw_path",
    "fetched_at",
)


def _row_to_dict(row: sqlite3.Row) -> dict:
    value = {field: row[field] for field in FIELDS if field in row.keys()}
    for field in ("numbers_json", "missing_json", "attachments_json"):
        try:
            value[field.removesuffix("_json")] = json.loads(value.pop(field) or "[]")
        except (json.JSONDecodeError, TypeError):
            value[field.removesuffix("_json")] = []
    for field in ("tax_enforcement", "law_library_hidden", "dedup_by_case_number", "needs_review"):
        value[field] = bool(value.get(field))
    return value


def export_jsonl(connection: sqlite3.Connection, path: str | Path) -> int:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with output.open("w", encoding="utf-8") as handle:
        for row in connection.execute("SELECT * FROM documents ORDER BY source_url, doc_type"):
            handle.write(json.dumps(_row_to_dict(row), ensure_ascii=False) + "\n")
            count += 1
    return count


def export_csv(connection: sqlite3.Connection, path: str | Path) -> int:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = [field for field in FIELDS if field not in {"body", "numbers_json", "missing_json", "attachments_json"}]
    count = 0
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in connection.execute("SELECT * FROM documents ORDER BY source_url, doc_type"):
            value = _row_to_dict(row)
            value["number"] = "；".join(value.get("numbers") or [value.get("number", "")])
            writer.writerow([value.get(column, "") for column in columns])
            count += 1
    return count


def _sql_string(value: str) -> str:
    return "N'" + str(value).replace("'", "''") + "'"


def _url_categories(connection: sqlite3.Connection) -> dict[str, set[int]]:
    mapping = source_url_categories(connection)
    if mapping:
        return mapping
    fallback: dict[str, set[int]] = defaultdict(set)
    for row in connection.execute(
        "SELECT source_url, category FROM documents WHERE source_url <> '' AND category IN (?,?,?)",
        (CATEGORY_EXTRA_TABLES, CATEGORY_NORMAL, CATEGORY_DEDUP),
    ):
        fallback[row["source_url"]].add(int(row["category"]))
    return fallback


def export_tax_url_sql(
    connection: sqlite3.Connection,
    path: str | Path,
    *,
    include_mixed: bool = False,
) -> tuple[int, int]:
    """生成 ``taxUrlCollect`` 种子 SQL。

    一个详情页如果同时包含多类文书，``taxUrlCollect.Category`` 无法表达，
    默认跳过并计入 mixed；这类页面应由本应用按文书拆分后单独采集。
    """

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    mapping = _url_categories(connection)
    written = 0
    mixed = 0
    lines = [
        "-- 由 apps/tax_docs 生成：税务文书 URL + Category 分类。",
        "-- Category：1=处理决定书（税务执法/法律库隐藏）；2=处罚事项告知书；3=处罚决定书（按案号去重）。",
        "-- 执行后由既有 api_spider.py 继续采集详情并写入加工库。",
        "",
    ]
    for url, categories in sorted(mapping.items()):
        if len(categories) > 1:
            mixed += 1
            if not include_mixed:
                continue
        category = min(categories)
        lines.extend(
            [
                f"IF NOT EXISTS (SELECT 1 FROM taxUrlCollect WHERE url = {_sql_string(url)})",
                "BEGIN",
                f"    INSERT INTO taxUrlCollect (url, flag, Category) VALUES ({_sql_string(url)}, 0, {category});",
                "END",
                "ELSE",
                "BEGIN",
                f"    UPDATE taxUrlCollect SET Category = {category} "
                f"WHERE url = {_sql_string(url)} AND (Category IS NULL OR Category = 0);",
                "END",
                "",
            ]
        )
        written += 1
    output.write_text("\n".join(lines), "utf-8")
    return written, mixed


def write_summary(connection: sqlite3.Connection, path: str | Path) -> None:
    from .store import stats

    data = stats(connection)
    lines = [
        "# 税务文书采集暂存统计",
        "",
        f"- 报告来源行：{data.get('total_sources', 0)}",
        f"- 解析后文书：{data.get('total_documents', 0)}",
        f"- 附件：{data.get('attachments', 0)}",
        "",
        "## 按分类",
        "",
    ]
    for category, count in sorted(data.get("documents_by_category", {}).items()):
        lines.append(f"- Category {category}：{count}")
    lines.extend(["", "## 按文书类型", ""])
    for doc_type, count in sorted(data.get("documents_by_type", {}).items()):
        lines.append(f"- {doc_type}：{count}")
    lines.extend(["", "## 按状态", ""])
    for status, count in sorted(data.get("documents_by_status", {}).items()):
        lines.append(f"- {status}：{count}")
    lines.extend(["", "## 按保真度", ""])
    for fidelity, count in sorted(data.get("fidelity", {}).items()):
        lines.append(f"- {fidelity or '(空)'}：{count}")
    Path(path).write_text("\n".join(lines) + "\n", "utf-8")

def export_mixed_sources(connection: sqlite3.Connection, path: str | Path) -> int:
    """导出同一 URL 含多类文书的清单，供独立采集/人工拆分。"""

    mapping = _url_categories(connection)
    mixed_urls = {url for url, categories in mapping.items() if len(categories) > 1}
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = {}
    for row in connection.execute(
        "SELECT url, province, report, numbers_json, raw_types, doc_types_json, party FROM sources WHERE url <> ''"
    ):
        url = row["url"]
        if url not in mixed_urls:
            continue
        value = rows.setdefault(
            url,
            {
                "url": url,
                "province": row["province"],
                "report": row["report"],
                "raw_types": row["raw_types"],
                "doc_types": row["doc_types_json"],
                "numbers": row["numbers_json"],
                "party": row["party"],
            },
        )
        for source_key, output_key in (
            ("raw_types", "raw_types"),
            ("doc_types_json", "doc_types"),
            ("numbers_json", "numbers"),
            ("party", "party"),
            ("province", "province"),
        ):
            current = row[source_key]
            if current and current not in value[output_key]:
                value[output_key] = (
                    f"{value[output_key]}; {current}" if value[output_key] else current
                )
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("url", "province", "raw_types", "doc_types", "numbers", "party", "report"),
        )
        writer.writeheader()
        for value in sorted(rows.values(), key=lambda item: item["url"]):
            writer.writerow(value)
    return len(rows)

