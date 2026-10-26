"""SQLite 暂存库。

报告行、详情解析结果、附件和运行状态都落盘，支持断点续跑和离线复核。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .classify import DocType, SourceRecord, classify_doc_type


TZ8 = timezone(timedelta(hours=8))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    record_key TEXT PRIMARY KEY,
    report TEXT NOT NULL,
    province TEXT NOT NULL,
    row_no INTEGER NOT NULL,
    publish_date TEXT NOT NULL,
    raw_types TEXT NOT NULL,
    numbers_json TEXT NOT NULL,
    party TEXT NOT NULL,
    agency TEXT NOT NULL,
    url TEXT NOT NULL,
    context TEXT NOT NULL,
    status TEXT NOT NULL,
    notes TEXT NOT NULL,
    doc_types_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sources_status ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_url ON sources(url);

CREATE TABLE IF NOT EXISTS documents (
    record_key TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    source_url TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    category INTEGER NOT NULL,
    tax_enforcement INTEGER NOT NULL,
    law_library_hidden INTEGER NOT NULL,
    document_type_label TEXT NOT NULL,
    dedup_by_case_number INTEGER NOT NULL,
    needs_review INTEGER NOT NULL,
    title TEXT NOT NULL,
    number TEXT NOT NULL,
    numbers_json TEXT NOT NULL,
    agency TEXT NOT NULL,
    penalty_date TEXT NOT NULL,
    promulgating_date TEXT NOT NULL,
    party TEXT NOT NULL,
    people TEXT NOT NULL,
    party_class TEXT NOT NULL,
    law_according TEXT NOT NULL,
    body TEXT NOT NULL,
    fidelity TEXT NOT NULL,
    page_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL,
    notes TEXT NOT NULL,
    missing_json TEXT NOT NULL,
    attachments_json TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_number ON documents(number);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_key TEXT NOT NULL,
    url TEXT NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,
    path TEXT NOT NULL,
    text TEXT NOT NULL,
    fidelity TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(record_key, url)
);

CREATE INDEX IF NOT EXISTS idx_attachments_record ON attachments(record_key);
"""


def now_text() -> str:
    return datetime.now(TZ8).replace(microsecond=0).isoformat()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(SCHEMA)
    return connection


def upsert_sources(connection: sqlite3.Connection, records: Iterable[SourceRecord]) -> int:
    timestamp = now_text()
    count = 0
    for record in records:
        connection.execute(
            """
            INSERT INTO sources (
                record_key, report, province, row_no, publish_date, raw_types,
                numbers_json, party, agency, url, context, status, notes,
                doc_types_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_key) DO UPDATE SET
                report=excluded.report,
                province=excluded.province,
                row_no=excluded.row_no,
                publish_date=excluded.publish_date,
                raw_types=excluded.raw_types,
                numbers_json=excluded.numbers_json,
                party=excluded.party,
                agency=excluded.agency,
                url=excluded.url,
                context=excluded.context,
                notes=excluded.notes,
                doc_types_json=excluded.doc_types_json,
                updated_at=excluded.updated_at
            """,
            (
                record.record_key,
                record.report,
                record.province,
                record.row_no,
                record.publish_date,
                record.raw_types,
                json.dumps(record.numbers, ensure_ascii=False),
                record.party,
                record.agency,
                record.url,
                record.context,
                record.status,
                record.notes,
                json.dumps([doc_type.value for doc_type in record.doc_types], ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        count += 1
    connection.commit()
    return count


def iter_sources(
    connection: sqlite3.Connection,
    *,
    status: str | None = "pending",
    limit: int = 0,
    province: str = "",
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM sources"
    clauses: list[str] = []
    parameters: list[object] = []
    if status:
        clauses.append("status = ?")
        parameters.append(status)
    if province:
        clauses.append("province LIKE ?")
        parameters.append(f"%{province}%")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY report, row_no"
    if limit:
        sql += " LIMIT ?"
        parameters.append(limit)
    return list(connection.execute(sql, parameters))


def set_source_status(
    connection: sqlite3.Connection,
    record_key: str,
    status: str,
    notes: str | None = None,
) -> None:
    if notes is None:
        connection.execute(
            "UPDATE sources SET status=?, updated_at=? WHERE record_key=?",
            (status, now_text(), record_key),
        )
    else:
        connection.execute(
            "UPDATE sources SET status=?, notes=?, updated_at=? WHERE record_key=?",
            (status, notes, now_text(), record_key),
        )
    connection.commit()


def get_document(connection: sqlite3.Connection, record_key: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM documents WHERE record_key=?", (record_key,)).fetchone()


def iter_documents(
    connection: sqlite3.Connection,
    *,
    category: int | None = None,
    status: str = "",
    limit: int = 0,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM documents"
    clauses: list[str] = []
    parameters: list[object] = []
    if category is not None:
        clauses.append("category = ?")
        parameters.append(category)
    if status:
        clauses.append("status = ?")
        parameters.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY source_url, doc_type"
    if limit:
        sql += " LIMIT ?"
        parameters.append(limit)
    return list(connection.execute(sql, parameters))


def upsert_document(connection: sqlite3.Connection, values: dict) -> None:
    columns = (
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
    row = {column: values.get(column, "") for column in columns}
    for key in (
        "category",
        "tax_enforcement",
        "law_library_hidden",
        "dedup_by_case_number",
        "needs_review",
    ):
        row[key] = int(row[key] or 0)
    for key in ("numbers_json", "missing_json", "attachments_json"):
        if not isinstance(row[key], str):
            row[key] = json.dumps(row[key] or [], ensure_ascii=False)
    row["fetched_at"] = row["fetched_at"] or now_text()
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{column}=excluded.{column}" for column in columns if column != "record_key")
    connection.execute(
        f"INSERT INTO documents ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(record_key) DO UPDATE SET {updates}",
        [row[column] for column in columns],
    )
    connection.commit()


def upsert_attachment(
    connection: sqlite3.Connection,
    *,
    record_key: str,
    url: str,
    filename: str,
    kind: str,
    path: str,
    text: str,
    fidelity: str,
    status: str,
) -> None:
    connection.execute(
        """
        INSERT INTO attachments (record_key, url, filename, kind, path, text, fidelity, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(record_key, url) DO UPDATE SET
            filename=excluded.filename,
            kind=excluded.kind,
            path=excluded.path,
            text=excluded.text,
            fidelity=excluded.fidelity,
            status=excluded.status
        """,
        (record_key, url, filename, kind, path, text, fidelity, status),
    )
    connection.commit()


def known_numbers(connection: sqlite3.Connection) -> set[str]:
    numbers: set[str] = set()
    for row in connection.execute("SELECT number, numbers_json FROM documents WHERE category=3"):
        if row["number"]:
            numbers.add(row["number"])
        try:
            for value in json.loads(row["numbers_json"] or "[]"):
                if value:
                    numbers.add(value)
        except json.JSONDecodeError:
            continue
    return numbers


def stats(connection: sqlite3.Connection) -> dict:
    result: dict[str, dict] = {}
    result["sources"] = {
        row["status"]: row["count"]
        for row in connection.execute("SELECT status, COUNT(*) AS count FROM sources GROUP BY status")
    }
    result["documents_by_category"] = {
        str(row["category"]): row["count"]
        for row in connection.execute("SELECT category, COUNT(*) AS count FROM documents GROUP BY category")
    }
    result["documents_by_type"] = {
        row["doc_type"]: row["count"]
        for row in connection.execute("SELECT doc_type, COUNT(*) AS count FROM documents GROUP BY doc_type")
    }
    result["documents_by_status"] = {
        row["status"]: row["count"]
        for row in connection.execute("SELECT status, COUNT(*) AS count FROM documents GROUP BY status")
    }
    result["fidelity"] = {
        row["fidelity"]: row["count"]
        for row in connection.execute("SELECT fidelity, COUNT(*) AS count FROM documents GROUP BY fidelity")
    }
    result["attachments"] = connection.execute("SELECT COUNT(*) FROM attachments").fetchone()[0]
    result["total_documents"] = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    result["total_sources"] = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    return result


def reset(connection: sqlite3.Connection, statuses: Iterable[str] = ("failed", "blocked")) -> int:
    placeholders = ", ".join("?" for _ in statuses)
    cursor = connection.execute(
        f"UPDATE sources SET status='pending', updated_at=? WHERE status IN ({placeholders})",
        [now_text(), *statuses],
    )
    connection.commit()
    return cursor.rowcount

def source_url_categories(connection: sqlite3.Connection) -> dict[str, set[int]]:
    """按来源行计算每个 URL 涉及的 Category 集合。

    这是给 ``taxUrlCollect`` 播种用的：不需要先跑详情采集。
    """

    from collections import defaultdict

    mapping: dict[str, set[int]] = defaultdict(set)
    for row in connection.execute("SELECT url, doc_types_json FROM sources WHERE url <> ''"):
        try:
            values = json.loads(row["doc_types_json"] or "[]")
        except json.JSONDecodeError:
            values = []
        for value in values:
            try:
                doc_type = DocType(value)
            except ValueError:
                continue
            category = classify_doc_type(doc_type).category
            if category:
                mapping[row["url"]].add(category)
    return mapping

