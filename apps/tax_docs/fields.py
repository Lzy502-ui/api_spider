"""规则化字段抽取。

输入只认「纯文本 + 来源元数据」，不依赖 AI，保证同一份文书每次抽取结果一致。
抽不到的字段一律留空并记录在 ``missing`` 中，不用相邻字段或常识补值。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .classify import split_case_numbers


_CN_DIGITS = {
    "〇": 0,
    "零": 0,
    "○": 0,
    "Ｏ": 0,
    "O": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

_DATE_RE = re.compile(
    r"(?P<year>[\d〇零○ＯO一二三四五六七八九十]{4})\s*[-/.年]\s*"
    r"(?P<month>[\d〇零○ＯO一二三四五六七八九十]{1,3})\s*[-/.月]\s*"
    r"(?P<day>[\d〇零○ＯO一二三四五六七八九十]{1,3})(?=日|[\s,，。；;）)]|$)\s*日?"
)
_DATE_LABEL_RE = re.compile(
    r"(?:发布日期|发布时间|公开日期|成文日期|发文日期)\s*[:：]?\s*"
    r"([\d〇零○ＯO一二三四五六七八九十]{4}\s*[-/.年]\s*[\d〇零○ＯO一二三四五六七八九十]{1,3}\s*[-/.月]\s*[\d〇零○ＯO一二三四五六七八九十]{1,3}(?=日|[\s,，。；;）)]|$)\s*日?)"
)
_CASE_NUMBER_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z]{0,24}[〔\[（(]\s*\d{4}\s*[〕\]）)]\s*\d{1,8}\s*号"
)
_AGENCY_RE = re.compile(
    r"(?:国家税务总局[\u4e00-\u9fa5]{0,40}?(?:税务局|税务所|稽查局|分局|稽查一局|稽查二局|稽查三局|稽查四局|稽查五局)"
    r"|[\u4e00-\u9fa5]{2,40}?(?:税务局|税务所|稽查局|分局))"
)
_LAW_REF_RE = re.compile(r"《(?P<law>[^》]{2,100})》(?P<after>(?:(?!《)[^。；\n]){0,160})")
_LAW_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百零〇\d]+条(?:第[（(]?[一二三四五六七八九十百零〇\d]+[）)]?[款项])*")

_UNIT_SUFFIXES = (
    "公司",
    "厂",
    "中心",
    "事务所",
    "合作社",
    "商行",
    "店",
    "矿",
    "院",
    "校",
    "站",
    "场",
    "部",
    "所",
    "局",
    "集团",
    "企业",
    "工作室",
    "经营部",
    "门市部",
    "社",
    "行",
    "馆",
    "酒店",
    "宾馆",
    "超市",
    "医院",
    "学校",
    "银行",
    "协会",
    "基金会",
    "街道",
    "社区",
)

_NOISE_PREFIXES = (
    "发布时间",
    "发布日期",
    "来源：",
    "来源:",
    "浏览次数",
    "字号",
    "打印本页",
    "分享至",
    "扫一扫",
    "附件：",
    "附件:",
)


def _to_int(token: str) -> int:
    token = token.strip()
    if token.isdigit():
        return int(token)
    values = [_CN_DIGITS.get(char) for char in token]
    values = [value for value in values if value is not None]
    if not values:
        return 0
    # 年份逐位拼接；月/日如「十一」需要按十位处理。
    if len(token) == 1:
        return values[0]
    if token == "十":
        return 10
    if "十" in token:
        if token.startswith("十"):
            return 10 + (values[-1] if values else 0)
        tens, _, ones = token.partition("十")
        return (values[0] if values else 1) * 10 + (_CN_DIGITS.get(ones, 0) if ones else 0)
    return int("".join(str(value) for value in values))


def normalize_date(value: str | None) -> str:
    """把常见日期写法转成 YYYY-MM-DD；失败返回空串。"""

    if not value:
        return ""
    match = _DATE_RE.search(str(value))
    if not match:
        return ""
    year = _to_int(match.group("year"))
    month = _to_int(match.group("month"))
    day = _to_int(match.group("day"))
    if not (1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def extract_publish_date(text: str, meta: dict[str, str]) -> str:
    for name in ("PubDate", "PublishDate", "pubDate", "date", "DC.date"):
        value = meta.get(name)
        if value:
            parsed = normalize_date(value)
            if parsed:
                return parsed
    match = _DATE_LABEL_RE.search(text or "")
    return normalize_date(match.group(1)) if match else ""


def extract_penalty_date(text: str) -> str:
    """取文末落款日期（最后 20 行内最后一个有效日期）。"""

    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    dates: list[str] = []
    for line in lines[-20:]:
        for match in _DATE_RE.finditer(line):
            parsed = normalize_date(match.group(0))
            if parsed:
                dates.append(parsed)
    return dates[-1] if dates else ""


def _clean_title(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"^[\s\-—_|]+|[\s\-—_|]+$", "", value)
    return value


def extract_title(text: str, meta: dict[str, str]) -> str:
    for name in ("ArticleTitle", "Title", "DC.title", "html_title"):
        value = meta.get(name)
        if value and len(value.strip()) >= 4:
            return _clean_title(value)
    for line in (text or "").splitlines()[:20]:
        line = line.strip()
        if not line or len(line) < 6 or len(line) > 200:
            continue
        if any(prefix in line for prefix in _NOISE_PREFIXES):
            continue
        if any(keyword in line for keyword in ("决定书", "告知书", "公告", "通知书")):
            return _clean_title(line)
    return ""


def extract_case_number(text: str, fallback_numbers: list[str] | None = None) -> str:
    if fallback_numbers:
        return fallback_numbers[0]
    match = _CASE_NUMBER_RE.search(text or "")
    return re.sub(r"\s+", "", match.group(0)) if match else ""


def extract_all_case_numbers(text: str, fallback_numbers: list[str] | None = None) -> list[str]:
    numbers = list(fallback_numbers or [])
    for number in split_case_numbers(text or ""):
        if number not in numbers:
            numbers.append(number)
    return numbers


def extract_agency(text: str, meta: dict[str, str]) -> str:
    """优先取文书正文中的具体机关，其次取来源元数据。

    公告页的 ``SiteName`` 往往只是市级税务局，不能代表作出文书的稽查局；
    因此先扫描正文中的独立机关行，并优先选择以稽查局/分局/税务所结尾的名称。
    """

    candidates: list[str] = []
    lines = (text or "").splitlines()
    for line in lines[:40] + lines[-20:]:
        value = line.strip()
        if not value or len(value) > 80:
            continue
        if "关于" in value:
            value = value.split("关于", 1)[0].strip()
        if "联系方式" in value:
            value = value.split("联系方式", 1)[-1].lstrip("：: ").strip()
        marker = value.find("国家税务总局")
        if marker > 0:
            value = value[marker:]
        value = value.strip("　 ：:，。；;、")
        if re.fullmatch(
            r"(?:国家税务总局)?[\u4e00-\u9fa5]{2,40}(?:稽查局|税务所|分局|税务局)",
            value,
        ):
            candidates.append(value)

    if candidates:
        specific = [value for value in candidates if value.endswith(("稽查局", "税务所", "分局"))]
        pool = specific or candidates
        return max(pool, key=len)

    for name in ("ContentSource", "SiteName", "Author", "source"):
        value = meta.get(name, "").strip()
        if value and value.endswith(("局", "所", "委员会", "厅", "办公室")):
            return value

    match = re.search(
        r"(?:国家税务总局)?[\u4e00-\u9fa5]{2,40}(?:稽查局|税务所|分局|税务局)",
        text or "",
    )
    return match.group(0) if match else ""


def _clean_party(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.split(r"[；;]", value)[0].strip()
    value = re.sub(r"^(?:受送达人|当事人|被处罚人|纳税人名称|单位名称)\s*[:：]\s*", "", value)
    value = re.sub(r"等\s*\d+\s*户(?:单位)?$", "", value).strip()
    value = re.sub(r"[,，]\s*(?:统一社会信用代码|纳税人识别号|身份证号).*$", "", value).strip()
    return value


def extract_party(text: str, fallback: str = "") -> str:
    if fallback:
        return _clean_party(fallback)
    patterns = (
        r"(?:受送达人|当事人|被处罚人|纳税人名称|单位名称)\s*[:：]\s*([^\n；;]{2,100})",
        r"([\u4e00-\u9fa5A-Za-z0-9（）()·]{2,80}(?:公司|厂|中心|事务所|合作社|商行|店|经营部|工作室))",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return _clean_party(match.group(1))
    return ""


def extract_people(text: str) -> str:
    match = re.search(r"(?:法定代表人|负责人|经营者|投资人)\s*[:：]\s*([\u4e00-\u9fa5]{2,4})", text or "")
    return match.group(1) if match else ""


def classify_party(party: str) -> str:
    if not party:
        return ""
    if any(suffix in party for suffix in _UNIT_SUFFIXES):
        return "法人及其他组织"
    clean = re.sub(r"（.*?）|\(.*?\)", "", party).strip()
    if 2 <= len(clean) <= 4 and re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", clean):
        return "个人"
    if "等" in party and ("户" in party or "单位" in party):
        return "法人及其他组织"
    return ""


def _law_groups(text: str) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for match in _LAW_REF_RE.finditer(text or ""):
        law = (match.group("law") or "").strip()
        if not law:
            continue
        if any(title in law for title in ("决定书", "告知书", "催告书", "通知书", "公告")):
            continue
        after = match.group("after") or ""
        articles = _LAW_ARTICLE_RE.findall(after)
        groups.setdefault(law, [])
        for article in articles:
            if article not in groups[law]:
                groups[law].append(article)
        if not articles and not groups[law]:
            groups[law].append("")
    return groups


def extract_law_according(text: str) -> str:
    groups = _law_groups(text)
    if not groups:
        return ""
    law_parts: list[str] = []
    for law, articles in groups.items():
        if articles and any(articles):
            law_parts.append(f"《{law}》" + "、".join(article for article in articles if article))
        else:
            law_parts.append(f"《{law}》")
    return "；".join(law_parts)


def _body_text(text: str, title: str, agency: str, number: str) -> str:
    if not text:
        return ""
    lines = [line.rstrip() for line in text.splitlines()]
    trimmed: list[str] = []
    head_budget = 8
    for index, line in enumerate(lines):
        stripped = line.strip()
        if index < head_budget:
            if not stripped:
                continue
            if stripped == title or stripped == agency or (number and number in stripped):
                continue
            if any(stripped.startswith(prefix) for prefix in _NOISE_PREFIXES):
                continue
        trimmed.append(line)
    result = "\n".join(trimmed)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


@dataclass
class ExtractedFields:
    title: str = ""
    number: str = ""
    numbers: list[str] = field(default_factory=list)
    agency: str = ""
    penalty_date: str = ""
    promulgating_date: str = ""
    party: str = ""
    people: str = ""
    party_class: str = ""
    law_according: str = ""
    body: str = ""
    missing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "adminPenaltyTitle": self.title,
            "adminPenaltyNumber": self.number,
            "adminPenaltyNumbers": self.numbers,
            "trialCourtNameOld": self.agency,
            "adminPenaltyDate": self.penalty_date,
            "promulgatingDate": self.promulgating_date,
            "punishedParties": self.party,
            "punishedPeople": self.people,
            "punishedPartyClass": self.party_class,
            "lawAccording": self.law_according,
            "body": self.body,
            "missing": self.missing,
        }


def extract_fields(
    text: str,
    meta: dict[str, str] | None = None,
    *,
    source_party: str = "",
    fallback_numbers: list[str] | None = None,
) -> ExtractedFields:
    meta = meta or {}
    text = text or ""
    title = extract_title(text, meta)
    numbers = extract_all_case_numbers(text, fallback_numbers)
    number = numbers[0] if numbers else ""
    agency = extract_agency(text, meta)
    publish_date = extract_publish_date(text, meta)
    penalty_date = extract_penalty_date(text)
    party = extract_party(text, source_party)
    people = extract_people(text)
    law = extract_law_according(text)
    body = _body_text(text, title, agency, number)

    missing: list[str] = []
    effective_date = penalty_date or publish_date
    for key, value in (
        ("adminPenaltyTitle", title),
        ("adminPenaltyNumber", number),
        ("trialCourtNameOld", agency),
        ("adminPenaltyDate", effective_date),
        ("punishedParties", party),
        ("lawAccording", law),
        ("body", body),
    ):
        if not value:
            missing.append(key)

    return ExtractedFields(
        title=title,
        number=number,
        numbers=numbers,
        agency=agency,
        penalty_date=penalty_date or publish_date,
        promulgating_date=publish_date,
        party=party,
        people=people,
        party_class=classify_party(party),
        law_according=law,
        body=body,
        missing=missing,
    )
