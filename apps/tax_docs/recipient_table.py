"""从公告正文表格中拆分「一户一行」的受送达人记录。

税务机关常用一张表同时公告多户纳税人，每一行都对应一份独立文书。
本模块只做表格结构识别，不解读正文。
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from .classify import split_case_numbers


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "tr":
            self._row = []
            self.rows.append(self._row)
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
            self._row.append("")
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            if self._cell is not None and self._row is not None:
                self._row[-1] = re.sub(r"\s+", " ", unescape("".join(self._cell))).strip()
            self._cell = None
        elif tag == "tr":
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _header_columns(row: list[str]) -> tuple[int, int] | None:
    party_index = number_index = -1
    for index, value in enumerate(row):
        if party_index < 0 and any(keyword in value for keyword in ("纳税人名称", "受送达人", "当事人", "单位名称", "姓名")):
            party_index = index
        if number_index < 0 and any(keyword in value for keyword in ("文书字轨号", "文件号", "文号", "案号", "文书号")):
            number_index = index
    if party_index >= 0 and number_index >= 0:
        return party_index, number_index
    return None


def extract_recipient_rows(html: str) -> list[dict]:
    """返回 ``[{party, numbers, row_no}]``；无法识别时返回空列表。"""

    results: list[dict] = []
    for table_match in re.finditer(r"<table\b[^>]*>[\s\S]*?</table>", html or "", flags=re.I):
        parser = _TableParser()
        parser.feed(table_match.group(0))
        rows = [row for row in parser.rows if any(cell for cell in row)]
        if len(rows) < 2:
            continue
        columns = None
        header_index = 0
        for index, row in enumerate(rows[:3]):
            columns = _header_columns(row)
            if columns:
                header_index = index
                break
        if not columns:
            continue
        party_index, number_index = columns
        for row in rows[header_index + 1 :]:
            if max(party_index, number_index) >= len(row):
                continue
            party = row[party_index].strip()
            numbers = split_case_numbers(row[number_index])
            if not numbers:
                continue
            results.append({"party": party, "numbers": numbers, "row_no": len(results) + 1})
    return results
