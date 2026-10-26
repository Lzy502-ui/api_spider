"""税务文书采集应用。"""

from .classify import (
    CATEGORY_EXTRA_TABLES,
    CATEGORY_NORMAL,
    CATEGORY_SKIP,
    CATEGORY_DEDUP,
    Classification,
    DocType,
    classify_doc_type,
    classify_raw_types,
    split_case_numbers,
)

__all__ = [
    "CATEGORY_EXTRA_TABLES",
    "CATEGORY_NORMAL",
    "CATEGORY_SKIP",
    "CATEGORY_DEDUP",
    "Classification",
    "DocType",
    "classify_doc_type",
    "classify_raw_types",
    "split_case_numbers",
]
