#!/usr/bin/env python3
"""税务文书采集命令行入口。

典型用法::

    python3 apps/tax_docs/cli.py import-reports --source 各省税务文书.zip
    python3 apps/tax_docs/cli.py collect --limit 10
    python3 apps/tax_docs/cli.py export --out-dir data/tax_docs/export
    MSSQL_HOST=... python3 apps/tax_docs/cli.py apply-urls --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.tax_docs.classify import DocType
from apps.tax_docs.collector import collect_pending
from apps.tax_docs.export import (
    export_csv,
    export_jsonl,
    export_mixed_sources,
    export_tax_url_sql,
    write_summary,
)
from apps.tax_docs.fetch import Fetcher
from apps.tax_docs.ingest import apply_tax_urls
from apps.tax_docs.report_parser import parse_report_source
from apps.tax_docs.store import (
    connect,
    iter_documents,
    known_numbers,
    reset,
    stats,
    upsert_sources,
)


DEFAULT_DB = "data/tax_docs/tax_docs.db"
DEFAULT_CACHE = "data/tax_docs/cache"
DEFAULT_ATTACHMENTS = "data/tax_docs/attachments"
DEFAULT_SOURCE = "data/tax_docs/各省税务文书.zip"


def _print_stats(connection) -> None:
    data = stats(connection)
    print(f"来源行：{data.get('total_sources', 0)}；文书：{data.get('total_documents', 0)}；附件：{data.get('attachments', 0)}")
    print("来源状态：" + json.dumps(data.get("sources", {}), ensure_ascii=False))
    print("文书分类：" + json.dumps(data.get("documents_by_category", {}), ensure_ascii=False))
    print("文书类型：" + json.dumps(data.get("documents_by_type", {}), ensure_ascii=False))
    print("文书状态：" + json.dumps(data.get("documents_by_status", {}), ensure_ascii=False))
    print("保真度：" + json.dumps(data.get("fidelity", {}), ensure_ascii=False))


def command_import(args) -> int:
    connection = connect(args.db)
    try:
        records = parse_report_source(args.source)
        inserted = upsert_sources(connection, records)
        print(f"已解析报告：{args.source}")
        print(f"写入来源行：{inserted}")
        by_type: dict[str, int] = {}
        by_province: dict[str, int] = {}
        with_url = 0
        for record in records:
            for doc_type in record.doc_types:
                by_type[doc_type.value] = by_type.get(doc_type.value, 0) + 1
            by_province[record.province] = by_province.get(record.province, 0) + 1
            if record.url:
                with_url += 1
        print(f"有原文链接：{with_url}；无链接：{len(records) - with_url}")
        print("文书类型：" + json.dumps(by_type, ensure_ascii=False))
        print("省份分布：" + json.dumps(by_province, ensure_ascii=False))
        _print_stats(connection)
    finally:
        connection.close()
    return 0


def _load_number_file(path: str) -> set[str]:
    if not path:
        return set()
    numbers: set[str] = set()
    for line in Path(path).read_text("utf-8").splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            numbers.add(value)
    return numbers


def command_collect(args) -> int:
    connection = connect(args.db)
    try:
        if args.retry:
            count = reset(connection, ("failed", "blocked", "empty_content"))
            print(f"已重置 {count} 条失败/拦截来源为 pending")
        numbers = known_numbers(connection) if args.dedup else set()
        numbers |= _load_number_file(args.known_numbers_file)
        fetcher = Fetcher(
            cache_dir=args.cache_dir,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            insecure=args.insecure,
        )
        summary = collect_pending(
            connection,
            fetcher,
            attachment_dir=args.attachment_dir,
            limit=args.limit,
            province=args.province,
            known_numbers=numbers,
            refresh=args.refresh,
        )
        print("采集结束：" + json.dumps(summary, ensure_ascii=False))
        _print_stats(connection)
    finally:
        connection.close()
    return 0


def command_stats(args) -> int:
    connection = connect(args.db)
    try:
        data = stats(connection)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            _print_stats(connection)
    finally:
        connection.close()
    return 0


def command_list(args) -> int:
    connection = connect(args.db)
    try:
        rows = iter_documents(
            connection,
            category=args.category,
            status=args.status,
            limit=args.limit,
        )
        for row in rows:
            if args.json:
                print(json.dumps({key: row[key] for key in row.keys() if key != "body"}, ensure_ascii=False))
            else:
                print(
                    f"{row['category']}\t{row['doc_type']}\t{row['status']}\t"
                    f"{row['number'] or '-'}\t{row['title'][:60] or '-'}\t{row['source_url']}"
                )
    finally:
        connection.close()
    return 0


def command_export(args) -> int:
    connection = connect(args.db)
    try:
        output_dir = Path(args.out_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        formats = {value.strip().lower() for value in args.formats.split(",") if value.strip()}
        if "jsonl" in formats:
            count = export_jsonl(connection, output_dir / "tax_documents.jsonl")
            print(f"JSONL：{count} 条 -> {output_dir / 'tax_documents.jsonl'}")
        if "csv" in formats:
            count = export_csv(connection, output_dir / "tax_documents.csv")
            print(f"CSV：{count} 条 -> {output_dir / 'tax_documents.csv'}")
        if "sql" in formats:
            written, mixed = export_tax_url_sql(
                connection,
                output_dir / "tax_url_collect.sql",
                include_mixed=args.include_mixed,
            )
            mixed_count = export_mixed_sources(connection, output_dir / "mixed_urls.csv")
            print(
                f"SQL：{written} 个 URL -> {output_dir / 'tax_url_collect.sql'}；"
                f"混合类型 {mixed_count} 个 -> {output_dir / 'mixed_urls.csv'}"
            )
        if "md" in formats:
            write_summary(connection, output_dir / "summary.md")
            print(f"统计：{output_dir / 'summary.md'}")
    finally:
        connection.close()
    return 0


def command_apply_urls(args) -> int:
    connection = connect(args.db)
    try:
        summary = apply_tax_urls(
            connection,
            include_mixed=args.include_mixed,
            dry_run=not args.apply,
        )
        print(json.dumps(summary, ensure_ascii=False))
        if not args.apply:
            print("当前为 dry-run；加 --apply 并配置 MSSQL_* 环境变量后写入 taxUrlCollect")
    finally:
        connection.close()
    return 0


def command_reset(args) -> int:
    connection = connect(args.db)
    try:
        statuses = tuple(value.strip() for value in args.statuses.split(",") if value.strip())
        count = reset(connection, statuses)
        print(f"已重置 {count} 条来源为 pending")
    finally:
        connection.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="各省税务文书采集与分类")
    parser.add_argument("--db", default=os.environ.get("TAX_DOC_DB", DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)

    import_parser = sub.add_parser("import-reports", help="解析报告 zip/目录并写入来源表")
    import_parser.add_argument(
        "--source", default=os.environ.get("TAX_DOC_SOURCE", DEFAULT_SOURCE)
    )
    import_parser.set_defaults(func=command_import)

    collect_parser = sub.add_parser("collect", help="采集待处理来源")
    collect_parser.add_argument("--limit", type=int, default=0)
    collect_parser.add_argument("--province", default="")
    collect_parser.add_argument("--delay", type=float, default=2.0)
    collect_parser.add_argument("--timeout", type=float, default=30.0)
    collect_parser.add_argument("--retries", type=int, default=2)
    collect_parser.add_argument("--cache-dir", default=DEFAULT_CACHE)
    collect_parser.add_argument("--attachment-dir", default=DEFAULT_ATTACHMENTS)
    collect_parser.add_argument("--refresh", action="store_true", help="忽略 HTTP 缓存")
    collect_parser.add_argument("--retry", action="store_true", help="把失败来源重置为 pending")
    collect_parser.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    collect_parser.add_argument("--dedup", action="store_true", default=True, help="处罚决定书按本地已采案号去重")
    collect_parser.add_argument("--no-dedup", dest="dedup", action="store_false")
    collect_parser.add_argument("--known-numbers-file", default="", help="外部已收录案号清单，一行一个")
    collect_parser.set_defaults(func=command_collect)

    stats_parser = sub.add_parser("stats", help="查看暂存统计")
    stats_parser.add_argument("--json", action="store_true")
    stats_parser.set_defaults(func=command_stats)

    list_parser = sub.add_parser("list", help="列出文书")
    list_parser.add_argument("--category", type=int, choices=(0, 1, 2, 3))
    list_parser.add_argument("--status", default="")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=command_list)

    export_parser = sub.add_parser("export", help="导出 JSONL/CSV/SQL/统计")
    export_parser.add_argument("--out-dir", default="data/tax_docs/export")
    export_parser.add_argument("--formats", default="jsonl,csv,sql,md")
    export_parser.add_argument("--include-mixed", action="store_true")
    export_parser.set_defaults(func=command_export)

    apply_parser = sub.add_parser("apply-urls", help="把 URL+Category 写入 SQL Server taxUrlCollect")
    apply_parser.add_argument("--apply", action="store_true", help="真正执行写库；默认 dry-run")
    apply_parser.add_argument("--include-mixed", action="store_true")
    apply_parser.set_defaults(func=command_apply_urls)

    reset_parser = sub.add_parser("reset", help="重置来源状态")
    reset_parser.add_argument("--statuses", default="failed,blocked,empty_content")
    reset_parser.set_defaults(func=command_reset)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
