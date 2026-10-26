"""文书类型与录入分类规则。

三条规则来自需求：

1. 处理决定书：全部新增，勾选「税务执法」和「法律库隐藏」，进入
   「监管措施/纪律处罚」分类；
2. 处罚事项告知书：全部新增，不勾选上述两项，进入「行政处罚」分类；
3. 处罚决定书：按案号去重后新增，不勾选上述两项，进入「行政处罚」分类。

Category 取值与既有 ``api_spider.py`` 保持一致，便于继续复用其采集与入库链路：

- 1：主流程 + ``docAndAllBigLibHidden`` + ``ap_adminPenaltyCaseExtend``；
- 2：仅主流程；
- 3：主流程 + 按发文字号去重。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum


CATEGORY_SKIP = 0
CATEGORY_EXTRA_TABLES = 1
CATEGORY_NORMAL = 2
CATEGORY_DEDUP = 3

DOCUMENT_TYPE_HANDLING = "监管措施/纪律处罚"
DOCUMENT_TYPE_PENALTY = "行政处罚"


class DocType(str, Enum):
    """规范化后的文书类型。"""

    HANDLING = "handling"
    NOTICE = "notice"
    PENALTY = "penalty"
    SKIP = "skip"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Classification:
    """单个文书类型对应的录入规则。"""

    doc_type: DocType
    category: int
    tax_enforcement: bool
    law_library_hidden: bool
    document_type_label: str
    dedup_by_case_number: bool
    needs_review: bool = False
    reason: str = ""


_RULES: dict[DocType, Classification] = {
    DocType.HANDLING: Classification(
        doc_type=DocType.HANDLING,
        category=CATEGORY_EXTRA_TABLES,
        tax_enforcement=True,
        law_library_hidden=True,
        document_type_label=DOCUMENT_TYPE_HANDLING,
        dedup_by_case_number=False,
    ),
    DocType.NOTICE: Classification(
        doc_type=DocType.NOTICE,
        category=CATEGORY_NORMAL,
        tax_enforcement=False,
        law_library_hidden=False,
        document_type_label=DOCUMENT_TYPE_PENALTY,
        dedup_by_case_number=False,
    ),
    DocType.PENALTY: Classification(
        doc_type=DocType.PENALTY,
        category=CATEGORY_DEDUP,
        tax_enforcement=False,
        law_library_hidden=False,
        document_type_label=DOCUMENT_TYPE_PENALTY,
        dedup_by_case_number=True,
    ),
    DocType.SKIP: Classification(
        doc_type=DocType.SKIP,
        category=CATEGORY_SKIP,
        tax_enforcement=False,
        law_library_hidden=False,
        document_type_label="",
        dedup_by_case_number=False,
        reason="不在本次三类文书范围内",
    ),
    DocType.UNKNOWN: Classification(
        doc_type=DocType.UNKNOWN,
        category=CATEGORY_SKIP,
        tax_enforcement=False,
        law_library_hidden=False,
        document_type_label="",
        dedup_by_case_number=False,
        needs_review=True,
        reason="无法判断文书类型，需人工确认",
    ),
}

# 兼容政府网站常见的全角括号、方括号与半角混用。
_CASE_NUMBER_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z]{0,24}[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*\d{1,8}\s*号"
)

_SEPARATORS_RE = re.compile(r"[\s,，、;；/|+＋]+")
_NORMALIZE_TABLE = str.maketrans(
    {
        "＋": "+",
        "﹢": "+",
        "（": "(",
        "）": ")",
        "〔": "[",
        "〕": "]",
        "【": "[",
        "】": "]",
    }
)


def normalize_text(value: str | None) -> str:
    """规范化报告中的类型文字，统一全角符号与空白。"""

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).translate(_NORMALIZE_TABLE)
    return _SEPARATORS_RE.sub(" ", text).strip()


def classify_doc_type(doc_type: DocType) -> Classification:
    """返回单个文书类型对应的分类规则。"""

    return _RULES[doc_type]


def _contains_any(text: str, *keywords: str) -> bool:
    return any(keyword in text for keyword in keywords)


def classify_raw_types(raw: str | None) -> list[DocType]:
    """把报告中的类型描述拆成规范化文书类型列表。

    一个公告可能同时包含多份文书（例如「处理决定书 + 处罚决定书」），
    因此返回列表而不是单一值。顺序尽量保持原文出现顺序。
    """

    text = normalize_text(raw)
    if not text:
        return [DocType.UNKNOWN]
    if text in {"待确认", "未知", "不明", "?"}:
        return [DocType.UNKNOWN]

    non_penalty = "不予处罚决定书" in text
    probe = text.replace("不予处罚决定书", " ")

    positions: list[tuple[int, DocType]] = []
    handling_at = probe.find("处理决定书")
    if handling_at < 0:
        handling_at = probe.find("税务处理决定书")
    notice_at = probe.find("处罚事项告知书")
    if notice_at < 0:
        notice_at = probe.find("告知书")
    penalty_at = probe.find("处罚决定书")
    if penalty_at < 0:
        penalty_at = probe.find("处罚决定书汇总表")

    if handling_at >= 0:
        positions.append((handling_at, DocType.HANDLING))
    if notice_at >= 0 and "处罚" in probe:
        positions.append((notice_at, DocType.NOTICE))
    if penalty_at >= 0:
        positions.append((penalty_at, DocType.PENALTY))

    if not positions and "处理" in probe and "处罚" in probe:
        positions.append((probe.find("处理"), DocType.HANDLING))
        positions.append((probe.find("处罚"), DocType.PENALTY))

    if not positions and non_penalty:
        return [DocType.SKIP]

    if not positions:
        return [DocType.UNKNOWN]

    positions.sort(key=lambda item: item[0])
    result = [doc_type for _, doc_type in positions]
    if non_penalty:
        result.append(DocType.SKIP)
    return list(dict.fromkeys(result))


def _normalize_digits(value: str) -> str:
    table = str.maketrans("０１２３４５６７８９", "0123456789")
    return str(value).translate(table)


def split_case_numbers(raw: str | None) -> list[str]:
    """从「文件号」单元格中提取案号，保留原文括号形制与顺序。

    政府报告常把同一文号的多个序号简写为「〔2025〕204-207号、212号」，
    这里会展开成独立案号，便于逐条去重。
    """

    if not raw:
        return []
    text = _normalize_digits(str(raw))
    numbers: list[str] = []
    base_re = re.compile(
        r"([\u4e00-\u9fa5A-Za-z]{0,24}[〔\[（(]\s*\d{4}\s*[〕\]）)])"
    )
    tail_re = re.compile(r"(\d{1,8})(?:\s*-\s*(\d{1,8}))?\s*号")
    for base_match in base_re.finditer(text):
        base = re.sub(r"\s+", "", base_match.group(1))
        tail = text[base_match.end() :]
        # 只消费紧随其后的「数字号/数字-数字号/、数字号」序列。
        pos = 0
        while pos < len(tail):
            while pos < len(tail) and tail[pos] in " \t\r\n、,，;；":
                pos += 1
            token = tail_re.match(tail, pos)
            if not token:
                break
            raw_start = token.group(1)
            start_value = int(raw_start)
            end_value = int(token.group(2)) if token.group(2) else start_value
            if end_value < start_value:
                start_value, end_value = end_value, start_value
            if end_value == start_value:
                number = f"{base}{raw_start}号"
                if number not in numbers:
                    numbers.append(number)
            elif end_value - start_value <= 50:
                width = len(raw_start)
                for value in range(start_value, end_value + 1):
                    number = f"{base}{str(value).zfill(width)}号"
                    if number not in numbers:
                        numbers.append(number)
            else:
                number = f"{base}{raw_start}号"
                if number not in numbers:
                    numbers.append(number)
            pos = token.end()

    if not numbers:
        for match in _CASE_NUMBER_RE.finditer(text):
            value = re.sub(r"\s+", "", match.group(0))
            if value and value not in numbers:
                numbers.append(value)
    return numbers


def canonical_case_number(value: str | None) -> str:
    """案号比对用的宽松形式（仅用于本地离线去重）。"""

    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = re.sub(r"\s+", "", text)
    for left, right in (("〔", "["), ("〕", "]"), ("（", "("), ("）", ")"), ("【", "["), ("】", "]")):
        text = text.replace(left, right)
    return text


def dedup_decision(
    numbers: list[str] | tuple[str, ...],
    existing_numbers: set[str] | None,
) -> str:
    """返回 ``new`` / ``all_duplicate`` / ``partial_duplicate`` / ``no_number``。

    处罚决定书使用该结果决定是否还有新增内容；没有案号时不能安全去重，
    按新增处理并交给人工复核。
    """

    cleaned = [canonical_case_number(number) for number in numbers if number]
    cleaned = [number for number in cleaned if number]
    if not cleaned:
        return "no_number"
    if not existing_numbers:
        return "new"
    existing = {canonical_case_number(number) for number in existing_numbers}
    hit = [number for number in cleaned if number in existing]
    if len(hit) == len(cleaned):
        return "all_duplicate"
    if hit:
        return "partial_duplicate"
    return "new"


@dataclass
class SourceRecord:
    """报告表格中的一行，尚未采集详情。"""

    record_key: str
    report: str
    province: str
    row_no: int
    publish_date: str
    raw_types: str
    numbers: list[str]
    party: str
    agency: str
    url: str
    context: str = ""
    status: str = "pending"
    notes: str = ""
    doc_types: list[DocType] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.doc_types:
            self.doc_types = classify_raw_types(self.raw_types)


def source_record_key(province: str, url: str, number: str, raw_types: str, row_no: int) -> str:
    """稳定记录键：同一 URL 的不同文书类型不互相覆盖。"""

    import hashlib

    payload = "\x1f".join(
        [province or "", url or "", number or "", normalize_text(raw_types), str(row_no)]
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
