"""把分类后的 URL 写入既有 SQL Server ``taxUrlCollect``。

只负责「URL + Category」播种，详情采集和 MySQL/SQL Server 加工库写入仍由
客户环境中的 ``api_spider.py`` 完成，避免复制两套入库逻辑。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Mapping

from .classify import CATEGORY_DEDUP, CATEGORY_EXTRA_TABLES, CATEGORY_NORMAL
from .store import source_url_categories


PROCESS_CATEGORIES = (CATEGORY_EXTRA_TABLES, CATEGORY_NORMAL, CATEGORY_DEDUP)


def mssql_settings(env: Mapping[str, str] | None = None) -> dict:
    env = env or os.environ
    required = ("MSSQL_HOST", "MSSQL_USER", "MSSQL_PASSWORD", "MSSQL_DATABASE")
    missing = [name for name in required if not env.get(name)]
    if missing:
        raise RuntimeError("缺少 SQL Server 环境变量：" + ", ".join(missing))
    return {
        "host": env["MSSQL_HOST"],
        "port": int(env.get("MSSQL_PORT", "1433")),
        "user": env["MSSQL_USER"],
        "password": env["MSSQL_PASSWORD"],
        "database": env["MSSQL_DATABASE"],
        "charset": env.get("MSSQL_CHARSET", "utf8"),
    }


def apply_tax_urls(
    connection: sqlite3.Connection,
    *,
    env: Mapping[str, str] | None = None,
    include_mixed: bool = False,
    dry_run: bool = True,
) -> dict[str, int]:
    mapping = source_url_categories(connection)
    if not mapping:
        for row in connection.execute(
            "SELECT source_url, category FROM documents WHERE source_url <> '' AND category IN (?,?,?)",
            PROCESS_CATEGORIES,
        ):
            mapping.setdefault(row["source_url"], set()).add(int(row["category"]))

    planned = {}
    mixed = 0
    for url, categories in mapping.items():
        if len(categories) > 1 and not include_mixed:
            mixed += 1
            continue
        planned[url] = min(categories)

    summary = {"planned": len(planned), "inserted": 0, "updated": 0, "mixed": mixed}
    if dry_run:
        return summary

    try:
        import pymssql  # type: ignore
    except ImportError as exc:
        raise RuntimeError("未安装 pymssql，无法写入 SQL Server；可先导出 SQL 人工执行") from exc

    settings = mssql_settings(env)
    database = pymssql.connect(
        host=settings["host"],
        port=settings["port"],
        user=settings["user"],
        password=settings["password"],
        database=settings["database"],
        charset=settings["charset"],
        timeout=120,
        login_timeout=30,
    )
    try:
        cursor = database.cursor()
        for url, category in planned.items():
            cursor.execute("SELECT id, Category FROM taxUrlCollect WHERE url = %s", (url,))
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    "INSERT INTO taxUrlCollect (url, flag, Category) VALUES (%s, 0, %s)",
                    (url, category),
                )
                summary["inserted"] += 1
            else:
                current = row[1]
                if current is None or int(current) == 0:
                    cursor.execute(
                        "UPDATE taxUrlCollect SET Category = %s WHERE id = %s",
                        (category, row[0]),
                    )
                    summary["updated"] += 1
        database.commit()
    except Exception:
        database.rollback()
        raise
    finally:
        database.close()
    return summary
