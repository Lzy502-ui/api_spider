"""HTML 正文定位与纯文本化。

原则：只在块级元素边界分段，行内标签不换行，``<br>`` 视为块内换行；
只删除明确命中的噪声行，不用「长度小于 N」之类的阈值误删正文。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin


VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}

BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "div",
    "dl",
    "dt",
    "dd",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}

SKIP_TAGS = {"script", "style", "noscript", "template"}

CONTAINER_SELECTORS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("id", ("zoom", "mainText", "js_content")),
    ("class", ("TRS_Editor", "article-content", "articleContent", "detail-content")),
    (
        "class",
        (
            "content",
            "article",
            "main-content",
            "wzcon",
            "text",
            "news_content",
            "view",
        ),
    ),
)

_ATTACHMENT_EXT_RE = re.compile(
    r"\.(pdf|docx?|xlsx?|wps|ofd|rtf|txt|csv|zip|rar|7z|jpe?g|png|gif|bmp|tiff?)$",
    re.I,
)
_NOISE_LINE_PATTERNS = (
    re.compile(r"^无障碍(浏览)?$"),
    re.compile(r"^页面原始配色$"),
    re.compile(r"^(蓝底黄字白链接|黄底黑字蓝链接|黑底黄字蓝链接|白底黑字蓝链接)$"),
    re.compile(r"^\[?\s*(大|中|小)\s*\]?$"),
    re.compile(r"^字号[:：]?$"),
    re.compile(r"^打印本页$"),
    re.compile(r"^分享至[:：]?$"),
    re.compile(r"^扫一扫在手机打开当前页$"),
    re.compile(r"^回到?顶部$"),
    re.compile(r"^网站地图$"),
    re.compile(r"^网站管理$"),
    re.compile(r"^联系我们$"),
    re.compile(r"^主办单位[:：].*"),
    re.compile(r"^地址[:：].*"),
    re.compile(r"^网站标识码[:：].*"),
    re.compile(r"^(粤|京|沪|津|渝|冀|豫|云|辽|黑|湘|皖|鲁|新|苏|浙|赣|鄂|桂|甘|晋|蒙|陕|吉|闽|贵|青|藏|川|宁|琼)ICP备.*"),
    re.compile(r"^.*公网安备.*$"),
    re.compile(r"^浏览次数[:：]?\s*\d*$"),
    re.compile(r"^来源[:：]?$"),
    re.compile(r"^发布时间[:：]?$"),
    re.compile(r"^当前位置[:：]?$"),
    re.compile(r"^首页\s*[>＞]\s*$"),
)
_TITLE_META_NAMES = ("ArticleTitle", "Title", "title", "DC.title")
_DATE_META_NAMES = ("PubDate", "PublishDate", "pubDate", "date")
_AGENCY_META_NAMES = ("ContentSource", "SiteName", "Author", "source")


@dataclass
class HtmlExtraction:
    text: str
    body_text: str
    container: str
    links: list[dict[str, str]] = field(default_factory=list)
    meta: dict[str, str] = field(default_factory=dict)
    page_kind: str = "unknown"

    @property
    def is_short(self) -> bool:
        return len(self.text) < 300


class _Frame:
    def __init__(self, tag: str, selector: str, start_depth: int) -> None:
        self.tag = tag
        self.selector = selector
        self.start_depth = start_depth
        self.parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.anchor: dict[str, str] | None = None


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.frames: list[_Frame] = []
        self.completed: list[_Frame] = []
        self.body_parts: list[str] = []
        self.body_links: list[dict[str, str]] = []
        self.body_anchor: dict[str, str] | None = None
        self.skip_depth = 0
        self.meta: dict[str, str] = {}
        self.title_parts: list[str] = []
        self.in_title = False

    def _selector(self, tag: str, attrs: dict[str, str | None]) -> str:
        for attr, values in CONTAINER_SELECTORS:
            raw = attrs.get(attr) or ""
            if attr == "id" and raw in values:
                return f"#{raw}"
            if attr == "class":
                classes = raw.split()
                for value in values:
                    if value in classes:
                        return f".{value}"
        return ""

    def _append(self, text: str) -> None:
        if self.skip_depth:
            return
        self.body_parts.append(text)
        for frame in self.frames:
            frame.parts.append(text)

    def _newline(self) -> None:
        if self.skip_depth:
            return
        self.body_parts.append("\n")
        for frame in self.frames:
            frame.parts.append("\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta":
            name = attributes.get("name") or attributes.get("property") or ""
            content = attributes.get("content") or ""
            if name and content:
                self.meta[name] = unescape(content).strip()
            return
        if tag == "title":
            self.in_title = True
        if tag in VOID_TAGS:
            if tag == "br":
                self._newline()
            return

        self.depth += 1
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        selector = self._selector(tag, attributes)
        if selector and not self.skip_depth:
            self.frames.append(_Frame(tag, selector, self.depth))
        if tag in BLOCK_TAGS:
            self._newline()
        if tag == "a":
            href = (attributes.get("href") or "").strip()
            self.body_anchor = {"href": href, "text": ""}
            for frame in self.frames:
                frame.anchor = {"href": href, "text": ""}

    def handle_endtag(self, tag: str) -> None:
        if tag == "meta" or tag in VOID_TAGS:
            return
        if tag == "title":
            self.in_title = False
        if tag in BLOCK_TAGS:
            self._newline()
        if tag == "a":
            if self.body_anchor is not None:
                if self.body_anchor["href"]:
                    self.body_links.append(self.body_anchor)
                self.body_anchor = None
            for frame in self.frames:
                if frame.anchor is not None:
                    if frame.anchor["href"]:
                        frame.links.append(frame.anchor)
                    frame.anchor = None

        for frame in list(self.frames):
            if frame.start_depth == self.depth:
                self.completed.append(frame)
                self.frames.remove(frame)

        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
        if self.skip_depth:
            return
        self._append(data)
        if self.body_anchor is not None:
            self.body_anchor["text"] += data
        for frame in self.frames:
            if frame.anchor is not None:
                frame.anchor["text"] += data


def _clean_lines(text: str) -> str:
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    kept: list[str] = []
    for line in lines:
        if not line:
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if any(pattern.match(line) for pattern in _NOISE_LINE_PATTERNS):
            continue
        kept.append(line)
    while kept and kept[0] == "":
        kept.pop(0)
    while kept and kept[-1] == "":
        kept.pop()
    return "\n".join(kept)


def _best_frame(extractor: _Extractor) -> _Frame | None:
    if not extractor.completed:
        return None
    priority = {}
    for index, (attr, values) in enumerate(CONTAINER_SELECTORS):
        for value in values:
            prefix = "#" if attr == "id" else "."
            priority[f"{prefix}{value}"] = index
    candidates = [frame for frame in extractor.completed if len("".join(frame.parts).strip()) >= 300]
    if not candidates:
        candidates = extractor.completed
    if not candidates:
        return None

    def rank(frame: _Frame) -> tuple[int, int]:
        return (priority.get(frame.selector, 99), -len("".join(frame.parts)))

    return sorted(candidates, key=rank)[0]


def extract_html_text(html: str) -> HtmlExtraction:
    """从详情页 HTML 中定位正文容器并生成纯文本。"""

    extractor = _Extractor()
    extractor.feed(html or "")
    frame = _best_frame(extractor)
    if frame is not None and len("".join(frame.parts).strip()) >= 200:
        text = _clean_lines("".join(frame.parts))
        container = frame.selector
        links = frame.links
    else:
        text = _clean_lines("".join(extractor.body_parts))
        container = "body"
        links = extractor.body_links

    body_text = _clean_lines("".join(extractor.body_parts))

    meta = dict(extractor.meta)
    if extractor.title_parts:
        meta.setdefault("html_title", re.sub(r"\s+", " ", "".join(extractor.title_parts)).strip())

    if len(text) >= 300:
        page_kind = "A_html"
    elif links:
        page_kind = "B_html_attachment"
    else:
        page_kind = "html_short"

    return HtmlExtraction(
        text=text,
        body_text=body_text,
        container=container,
        links=links,
        meta=meta,
        page_kind=page_kind,
    )


def detect_attachment_urls(html: str, base_url: str) -> list[dict[str, str]]:
    """识别详情页中的附件下载链接。"""

    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html or "",
        flags=re.I | re.S,
    ):
        href = unescape(match.group(1)).strip()
        if not href or href.lower().startswith(("javascript:", "#", "mailto:")):
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", unescape(match.group(2)))).strip()
        path = href.split("?")[0]
        if not (_ATTACHMENT_EXT_RE.search(path) or "附件" in text or "下载" in text):
            continue
        absolute = urljoin(base_url, href)
        if absolute in seen:
            continue
        seen.add(absolute)
        results.append({"url": absolute, "text": text})
    return results


def meta_value(meta: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        value = meta.get(name)
        if value:
            return value
    for key, value in meta.items():
        if key.lower() in {name.lower() for name in names} and value:
            return value
    return ""
