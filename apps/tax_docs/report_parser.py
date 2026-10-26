"""解析客户提供的省级税务局「税务文书筛选综合报告」HTML。

报告文件本身不是文书详情页，而是筛选结果表格。本模块只负责把表格里的
日期、文件号、文书类型、受送达人和原文链接提取成 :class:`SourceRecord`，
不访问网络，也不依赖第三方 HTML 库。
"""

from __future__ import annotations

import re
import zipfile
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

from .classify import (
    DocType,
    SourceRecord,
    classify_raw_types,
    source_record_key,
    split_case_numbers,
)

_REPORT_SUFFIXES = (".html", ".htm")
_BLOCK_BREAK = " "
_DATE_RE = re.compile(r"\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_CASE_NUMBER_HINT_RE = re.compile(r"[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*\d{1,8}\s*号")

_TYPE_KEYWORDS = (
    ("告知书", DocType.NOTICE),
    ("处理决定书", DocType.HANDLING),
    ("处罚决定书", DocType.PENALTY),
)


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


def iter_report_files(source: str | Path) -> Iterator[tuple[str, str]]:
    """遍历 .zip 或目录中的报告 HTML，产出 ``(文件名, 文本)``。"""

    path = Path(source)
    if path.is_dir():
        for file_path in sorted(path.rglob("*")):
            if file_path.suffix.lower() in _REPORT_SUFFIXES:
                yield str(file_path.relative_to(path)), decode_bytes(file_path.read_bytes())
        return

    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(_REPORT_SUFFIXES):
                continue
            yield _decode_zip_name(info), decode_bytes(archive.read(info))


def province_from_filename(name: str) -> str:
    stem = Path(name).stem
    prefix = stem.split("_", 1)[0]
    prefix = re.sub(r"^\d+个", "", prefix)
    return prefix.strip() or stem


def strip_tags(value: str) -> str:
    value = re.sub(r"<br\s*/?>", _BLOCK_BREAK, value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", unescape(value)).strip()


class _TableParser(HTMLParser):
    """把单个 ``<table>`` 解析成表头和单元格。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[dict]] = []
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._anchor: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self._row = []
            self.rows.append(self._row)
            return
        if tag in ("td", "th"):
            if self._row is None:
                self._row = []
                self.rows.append(self._row)
            self._cell = {
                "text": "",
                "links": [],
                "classes": attributes.get("class") or "",
                "tag": tag,
            }
            self._row.append(self._cell)
            return
        if tag == "a" and self._cell is not None:
            self._anchor = {"href": attributes.get("href") or "", "text": ""}
            return
        if tag == "br" and self._cell is not None:
            self._cell["text"] += _BLOCK_BREAK

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._cell = None
        elif tag == "tr":
            self._row = None
        elif tag == "a" and self._anchor is not None:
            if self._cell is not None:
                self._cell["links"].append(self._anchor)
            self._anchor = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell["text"] += data
        if self._anchor is not None:
            self._anchor["text"] += data


def _cell_text(cell: dict | None) -> str:
    if not cell:
        return ""
    return re.sub(r"\s+", " ", unescape(cell.get("text") or "")).strip()


def _first_http_link(cell: dict | None) -> tuple[str, str, str]:
    """返回 ``(url, 链接文字, 原始 href)``。"""

    if not cell:
        return "", "", ""
    for anchor in cell.get("links", []):
        href = unescape(anchor.get("href") or "").strip()
        if href.lower().startswith(("http://", "https://")):
            return href, _cell_text({"text": anchor.get("text") or ""}), href
        if href:
            return "", _cell_text({"text": anchor.get("text") or ""}), href
    return "", "", ""


def _header_map(headers: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        clean = header.replace(" ", "")
        for key in (
            "序号",
            "日期",
            "时间",
            "文件号",
            "文号",
            "文书类型",
            "类型",
            "受送达人",
            "案件名称",
            "当事人",
            "标题",
            "发布机关",
            "处罚机关",
            "原文链接",
            "链接",
        ):
            if key in clean and key not in mapping:
                mapping[key] = index
                break
    return mapping


def _pick(mapping: dict[str, int], row: list[dict], *keys: str) -> dict | None:
    for key in keys:
        index = mapping.get(key)
        if index is not None and index < len(row):
            return row[index]
    return None


def _looks_like_header(row: list[dict]) -> bool:
    text = " ".join(_cell_text(cell) for cell in row)
    return ("日期" in text or "时间" in text) and ("文件号" in text or "文号" in text)


def _looks_like_doc_table(headers: list[str]) -> bool:
    text = " ".join(headers)
    has_date = "日期" in text or "时间" in text
    has_number = "文件号" in text or "文号" in text
    has_link = "链接" in text or "原文" in text
    return has_date and has_number and has_link


def _section_context(prefix: str) -> str:
    parts: list[str] = []
    for pattern in (
        r'<div[^>]+class="[^"]*section-title[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]+class="[^"]*district-header[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]+class="[^"]*district-meta[^"]*"[^>]*>(.*?)</div>',
    ):
        matches = re.findall(pattern, prefix, flags=re.I | re.S)
        if matches:
            parts.append(strip_tags(matches[-1]))
    return " | ".join(part for part in parts if part)


def _infer_types(context: str, row_text: str) -> tuple[str, list[DocType]]:
    probe = f"{context} | {row_text}"
    for keyword, doc_type in _TYPE_KEYWORDS:
        if keyword in probe:
            label = {
                DocType.NOTICE: "处罚事项告知书",
                DocType.HANDLING: "处理决定书",
                DocType.PENALTY: "处罚决定书",
            }[doc_type]
            return label, [doc_type]
    return "", [DocType.UNKNOWN]


def _detect_columns_without_header(row: list[dict]) -> tuple[str, str, str, str, str, str]:
    """没有表头时按内容猜列（深圳报告中存在单行表格）。"""

    date = number = types = party = agency = url = ""
    for cell in row:
        text = _cell_text(cell)
        if not date and _DATE_RE.search(text) and len(text) <= 24:
            date = text
            continue
        if not number and _CASE_NUMBER_HINT_RE.search(text):
            number = text
            continue
        if not types and any(keyword in text for keyword, _ in _TYPE_KEYWORDS):
            types = text
            continue
        link, _, _ = _first_http_link(cell)
        if link and not url:
            url = link
            continue
        if not party and text and not text.startswith("查看"):
            party = text
    return date, number, types, party, agency, url


def _parse_table(
    table_html: str,
    *,
    report: str,
    province: str,
    context: str,
    start_row_no: int,
) -> list[SourceRecord]:
    parser = _TableParser()
    parser.feed(table_html)
    rows = [row for row in parser.rows if row]
    if not rows:
        return []

    headers: list[str] = []
    first_text = " ".join(_cell_text(cell) for cell in rows[0])
    if _looks_like_header(rows[0]) or any(cell.get("tag") == "th" for cell in rows[0]):
        headers = [_cell_text(cell) for cell in rows[0]]
        rows = rows[1:]

    mapping = _header_map(headers)
    doc_table = _looks_like_doc_table(headers)
    header_context = " | ".join(headers)
    full_context = " | ".join(part for part in (context, header_context) if part)

    records: list[SourceRecord] = []
    last_date = ""
    last_url = ""
    last_agency = ""
    row_no = start_row_no

    for row in rows:
        if not doc_table:
            # 没有可靠表头时，只接受同时含有日期、案号和链接的行。
            date, number, types, party, agency, url = _detect_columns_without_header(row)
            if not (date and number and (url or types)):
                continue
        else:
            date_cell = _pick(mapping, row, "日期", "时间")
            number_cell = _pick(mapping, row, "文件号", "文号")
            type_cell = _pick(mapping, row, "文书类型", "类型")
            party_cell = _pick(mapping, row, "受送达人", "案件名称", "当事人", "标题")
            agency_cell = _pick(mapping, row, "发布机关", "处罚机关")
            url_cell = _pick(mapping, row, "原文链接", "链接")

            date = _cell_text(date_cell)
            number = _cell_text(number_cell)
            types = _cell_text(type_cell)
            party = _cell_text(party_cell)
            agency = _cell_text(agency_cell)
            url, _, raw_href = _first_http_link(url_cell)

            if not url and raw_href and not raw_href.lower().startswith(("http://", "https://")):
                # URL 未获取、图片版等说明，保留在备注中，避免误当成链接。
                pass
            if url_cell and not url:
                raw_text = _cell_text(url_cell)
                if raw_text and not types:
                    pass

        row_text = " ".join(_cell_text(cell) for cell in row)
        if not types:
            types, inferred = _infer_types(full_context, row_text)
            if not types:
                continue
        if not agency:
            agency_match = re.search(r"(?:发布机关|处罚机关)[:：]\s*([^|；;]+)", full_context)
            if agency_match:
                agency = agency_match.group(1).strip()

        # 合并单元格：日期、链接、发布机关沿用上一行；案号与受送达人不沿用。
        if not date and last_date:
            date = last_date
        if not url and last_url and not _cell_text(_pick(mapping, row, "原文链接", "链接") if doc_table else None):
            url = last_url
        if not agency and last_agency:
            agency = last_agency
        if date:
            last_date = date
        if url:
            last_url = url
        if agency:
            last_agency = agency

        numbers = split_case_numbers(number)
        if not numbers and number and not _CASE_NUMBER_HINT_RE.search(number):
            # 例如「附件清单（6户）」「文号见附件」，保留原文但标记待核对。
            pass

        if not number and not party:
            continue

        notes = ""
        if not url:
            notes = "报告未提供原文链接"
        if "附件" in number and not numbers:
            notes = (notes + "；" if notes else "") + "批量附件，需按附件拆分"

        doc_types = classify_raw_types(types)
        records.append(
            SourceRecord(
                record_key=source_record_key(province, url, number, types, row_no),
                report=report,
                province=province,
                row_no=row_no,
                publish_date=date,
                raw_types=types,
                numbers=numbers,
                party=party,
                agency=agency,
                url=url,
                context=full_context,
                status="pending" if url else "url_missing",
                notes=notes,
                doc_types=doc_types,
            )
        )
        row_no += 1

    return records


def parse_report_html(html: str, report: str) -> list[SourceRecord]:
    """解析单个报告 HTML。"""

    province = province_from_filename(report)
    records: list[SourceRecord] = []
    table_re = re.compile(r"<table\b[^>]*>[\s\S]*?</table>", re.I)
    row_no = 0
    for match in table_re.finditer(html):
        context = _section_context(html[: match.start()])
        table_records = _parse_table(
            match.group(0),
            report=report,
            province=province,
            context=context,
            start_row_no=row_no + 1,
        )
        records.extend(table_records)
        row_no += len(table_records)
    return records


def parse_report_source(source: str | Path) -> list[SourceRecord]:
    """解析 .zip 或目录中的全部报告。"""

    records: list[SourceRecord] = []
    for name, text in iter_report_files(source):
        records.extend(parse_report_html(text, name))
    return records
