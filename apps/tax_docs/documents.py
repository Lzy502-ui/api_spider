"""文档形态识别与文本提取（Word / PDF / 图片 / HTML 附件）。

依赖全部按需导入：没有安装 PDF、Word 或 OCR 依赖时，程序不会崩溃，
而是返回空文本并记录 ``notes``，由调用方标记为待人工处理。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .html_text import extract_html_text


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_RTF_MAGIC = b"{\\rtf"
_IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF8", "gif"),
    (b"BM", "bmp"),
    (b"II*\x00", "tiff"),
    (b"MM\x00*", "tiff"),
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_EXISTING_NUMBER_RE = re.compile(
    r"^(?:[一二三四五六七八九十百]+、|（[一二三四五六七八九十百]+）|\([一二三四五六七八九十百]+\)|\d+[.、])\s*"
)


@dataclass
class TextDoc:
    text: str = ""
    fidelity: str = ""  # text-layer / docx / ocr / ocr-required / unsupported
    kind: str = ""  # A_html / B_html_attachment / C_docx / D_pdf / E_ocr
    notes: list[str] = field(default_factory=list)
    attachments: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def _chinese_number(value: int) -> str:
    digits = "零一二三四五六七八九"
    if value <= 0:
        return str(value)
    if value < 10:
        return digits[value]
    if value < 20:
        return "十" + (digits[value % 10] if value % 10 else "")
    if value < 100:
        tens, ones = divmod(value, 10)
        return digits[tens] + "十" + (digits[ones] if ones else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        text = digits[hundreds] + "百"
        if rest == 0:
            return text
        if rest < 10:
            return text + "零" + digits[rest]
        return text + _chinese_number(rest)
    return str(value)


def _format_counter(value: int, fmt: str) -> str:
    if fmt in {"chineseCounting", "japaneseCounting", "taiwaneseCounting", "chineseCountingThousand"}:
        return _chinese_number(value)
    if fmt in {"lowerLetter"}:
        return chr(ord("a") + (value - 1) % 26)
    if fmt in {"upperLetter"}:
        return chr(ord("A") + (value - 1) % 26)
    if fmt in {"decimalEnclosedCircle", "decimalEnclosedCircleChinese"}:
        return chr(0x2460 + (value - 1) % 20)
    if fmt in {"bullet"}:
        return ""
    return str(value)


class _Numbering:
    def __init__(self) -> None:
        self.levels: dict[str, dict[int, dict]] = {}
        self.num_to_abstract: dict[str, str] = {}
        self.counters: dict[str, list[int]] = {}

    def load(self, root: ET.Element) -> None:
        for abstract in root.findall(W + "abstractNum"):
            abstract_id = abstract.get(W + "abstractNumId") or ""
            levels: dict[int, dict] = {}
            for level in abstract.findall(W + "lvl"):
                ilvl = int(level.get(W + "ilvl") or 0)
                fmt_node = level.find(W + "numFmt")
                text_node = level.find(W + "lvlText")
                start_node = level.find(W + "start")
                levels[ilvl] = {
                    "fmt": fmt_node.get(W + "val") if fmt_node is not None else "decimal",
                    "text": text_node.get(W + "val") if text_node is not None else "",
                    "start": int(start_node.get(W + "val") or 1) if start_node is not None else 1,
                }
            self.levels[abstract_id] = levels
        for num in root.findall(W + "num"):
            num_id = num.get(W + "numId") or ""
            abstract = num.find(W + "abstractNumId")
            if abstract is not None:
                self.num_to_abstract[num_id] = abstract.get(W + "val") or ""

    def prefix(self, num_id: str, ilvl: int) -> str:
        abstract_id = self.num_to_abstract.get(num_id)
        levels = self.levels.get(abstract_id or "")
        if not levels:
            return ""
        counter = self.counters.setdefault(num_id, [0] * 9)
        start = levels.get(ilvl, {}).get("start", 1)
        counter[ilvl] = counter[ilvl] + 1 if counter[ilvl] else start
        for deeper in range(ilvl + 1, 9):
            counter[deeper] = 0
        level = levels.get(ilvl, {})
        template = level.get("text") or ""
        if not template:
            return ""
        for index in range(9):
            placeholder = f"%{index + 1}"
            if placeholder not in template:
                continue
            level_fmt = levels.get(index, {}).get("fmt", "decimal")
            value = counter[index] or levels.get(index, {}).get("start", 1)
            template = template.replace(placeholder, _format_counter(value, level_fmt))
        if level.get("fmt") == "bullet":
            return "• "
        return template


def _paragraph_text(paragraph: ET.Element, numbering: _Numbering) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        if node.tag == W + "t" and node.text:
            parts.append(node.text)
        elif node.tag in {W + "br", W + "cr"}:
            parts.append("\n")
        elif node.tag == W + "tab":
            parts.append("\t")
    text = "".join(parts).strip()
    if not text:
        return ""

    num_pr = paragraph.find(f"{W}pPr/{W}numPr")
    if num_pr is not None:
        num_id_node = num_pr.find(W + "numId")
        ilvl_node = num_pr.find(W + "ilvl")
        if num_id_node is not None:
            num_id = num_id_node.get(W + "val") or ""
            ilvl = int(ilvl_node.get(W + "val") or 0) if ilvl_node is not None else 0
            prefix = numbering.prefix(num_id, ilvl)
            if prefix and not _EXISTING_NUMBER_RE.match(text):
                text = prefix + text
    return text


def _table_text(table: ET.Element, numbering: _Numbering) -> str:
    lines: list[str] = []
    for row in table.findall(W + "tr"):
        cells: list[str] = []
        for cell in row.findall(W + "tc"):
            cell_parts: list[str] = []
            for paragraph in cell.iter(W + "p"):
                value = _paragraph_text(paragraph, numbering)
                if value:
                    cell_parts.append(value)
            cells.append(" ".join(cell_parts).strip())
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _walk_block(container: ET.Element, numbering: _Numbering) -> list[str]:
    parts: list[str] = []
    for child in list(container):
        if child.tag == W + "p":
            value = _paragraph_text(child, numbering)
            if value:
                parts.append(value)
        elif child.tag == W + "tbl":
            value = _table_text(child, numbering)
            if value:
                parts.append(value)
        elif child.tag == W + "sdt":
            content = child.find(W + "sdtContent")
            if content is not None:
                parts.extend(_walk_block(content, numbering))
        elif child.tag == W + "sectPr":
            continue
        else:
            # 部分模板会把正文放在自定义容器中，继续向下找段落。
            parts.extend(_walk_block(child, numbering))
    return parts


def docx_to_text(path: str | Path) -> TextDoc:
    """读取 .docx 正文，保留自动编号、表格，忽略页眉页脚。"""

    try:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
            numbering = _Numbering()
            if "word/numbering.xml" in archive.namelist():
                numbering.load(ET.fromstring(archive.read("word/numbering.xml")))
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        return TextDoc(notes=[f"docx 读取失败：{exc}"], fidelity="unsupported", kind="C_docx")

    try:
        root = ET.fromstring(document_xml)
    except ET.ParseError as exc:
        return TextDoc(notes=[f"document.xml 解析失败：{exc}"], fidelity="unsupported", kind="C_docx")

    body = root.find(W + "body")
    if body is None:
        return TextDoc(notes=["docx 缺少 body"], fidelity="unsupported", kind="C_docx")
    lines = _walk_block(body, numbering)
    return TextDoc(text="\n".join(lines), fidelity="docx", kind="C_docx")


def _convert_with_libreoffice(path: Path) -> Path | None:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return None
    output_dir = Path(tempfile.mkdtemp(prefix="tax-doc-"))
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "docx", "--outdir", str(output_dir), str(path)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    converted = output_dir / (path.stem + ".docx")
    return converted if converted.exists() else None


def doc_to_text(path: str | Path) -> TextDoc:
    """读取 .doc：优先 LibreOffice 转 docx；否则尝试 antiword。"""

    source = Path(path)
    converted = _convert_with_libreoffice(source)
    if converted is not None:
        result = docx_to_text(converted)
        result.kind = "C_doc"
        return result

    antiword = shutil.which("antiword")
    if antiword:
        try:
            completed = subprocess.run(
                [antiword, str(source)], check=True, capture_output=True, timeout=60
            )
            text = completed.stdout.decode("utf-8", errors="ignore")
            return TextDoc(text=text, fidelity="docx", kind="C_doc")
        except (subprocess.SubprocessError, OSError) as exc:
            return TextDoc(notes=[f"antiword 读取失败：{exc}"], kind="C_doc")

    return TextDoc(
        notes=["缺少 LibreOffice/antiword，无法读取 .doc"],
        fidelity="unsupported",
        kind="C_doc",
    )


def has_text_layer(text: str) -> bool:
    """按技术要点判断 PDF 文本层是否真的可用。"""

    if not text or len(text) < 50 or "(cid:" in text:
        return False
    cjk = len(_CJK_RE.findall(text))
    return cjk / max(len(text), 1) > 0.3


def _ocr_image(path: str | Path) -> tuple[str, str]:
    """返回 ``(text, engine)``；没有可用 OCR 时返回空。"""

    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        engine = RapidOCR()
        result, _ = engine(str(path))
        if result:
            return "\n".join(item[1] for item in result), "rapidocr"
    except Exception:
        pass

    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            return pytesseract.image_to_string(image, lang="chi_sim"), "tesseract"
    except Exception:
        pass
    return "", ""


def pdf_to_text(path: str | Path) -> TextDoc:
    """读取 PDF；文本层不可用时尝试 OCR，失败则标记 ocr-required。"""

    source = Path(path)
    try:
        import fitz  # type: ignore
    except ImportError:
        fitz = None

    if fitz is not None:
        try:
            document = fitz.open(str(source))
        except Exception as exc:
            return TextDoc(notes=[f"PDF 打开失败：{exc}"], kind="D_pdf", fidelity="unsupported")

        page_texts: list[str] = []
        ocr_notes: list[str] = []
        pages_needing_ocr = 0
        try:
            for page in document:
                text = page.get_text("text") or ""
                if has_text_layer(text):
                    page_texts.append(text)
                    continue
                pages_needing_ocr += 1
                image_path = None
                try:
                    pixmap = page.get_pixmap(dpi=300)
                    fd, image_path = tempfile.mkstemp(suffix=".png")
                    os.close(fd)
                    pixmap.save(image_path)
                    ocr_text, engine = _ocr_image(image_path)
                    if ocr_text:
                        page_texts.append(ocr_text)
                        ocr_notes.append(f"第 {page.number + 1} 页使用 {engine} OCR")
                    else:
                        page_texts.append("")
                finally:
                    if image_path and os.path.exists(image_path):
                        os.unlink(image_path)
        finally:
            document.close()

        text = "\n".join(page_texts).strip()
        if text:
            fidelity = "ocr" if pages_needing_ocr else "text-layer"
            kind = "E_ocr" if pages_needing_ocr else "D_pdf"
            return TextDoc(text=text, fidelity=fidelity, kind=kind, notes=ocr_notes)
        return TextDoc(
            notes=["PDF 无可用文本层，且未安装 OCR 引擎"],
            fidelity="ocr-required",
            kind="E_ocr",
        )

    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(source)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        if has_text_layer(text):
            return TextDoc(text=text, fidelity="text-layer", kind="D_pdf")
        return TextDoc(
            notes=["PDF 无可用文本层，且未安装 OCR 引擎"],
            fidelity="ocr-required",
            kind="E_ocr",
        )
    except ImportError:
        return TextDoc(
            notes=["缺少 PyMuPDF/pdfplumber，无法读取 PDF"],
            fidelity="unsupported",
            kind="D_pdf",
        )
    except Exception as exc:
        return TextDoc(notes=[f"PDF 读取失败：{exc}"], fidelity="unsupported", kind="D_pdf")


def _sniff(path: Path) -> tuple[str, str]:
    try:
        head = path.read_bytes()[:8192]
    except OSError:
        return "unknown", ""
    if head.startswith(b"%PDF"):
        return "pdf", "pdf"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
        except Exception:
            return "ooxml", ""
        if any(name.startswith("word/") for name in names):
            return "ooxml", "docx"
        if any(name.startswith("xl/") for name in names):
            return "ooxml", "xlsx"
        return "ooxml", ""
    if head.startswith(_OLE_MAGIC):
        raw = path.read_bytes()
        if b"WordDocument" in raw or "WordDocument".encode("utf-16-le") in raw:
            return "ole", "doc"
        if b"Workbook" in raw or b"Book" in raw:
            return "ole", "xls"
        return "ole", ""
    if head.startswith(_RTF_MAGIC):
        return "rtf", "rtf"
    for magic, name in _IMAGE_MAGIC:
        if head.startswith(magic):
            return "image", name
    lowered = head[:1024].lower()
    if b"<html" in lowered or b"<!doctype html" in lowered:
        return "html", "html"
    return "text", "text"


def extract_attachment_text(path: str | Path) -> TextDoc:
    """按真实文件类型提取附件文本。"""

    source = Path(path)
    if not source.exists():
        return TextDoc(notes=["附件不存在"], fidelity="unsupported")
    kind, subtype = _sniff(source)
    try:
        if kind == "pdf":
            return pdf_to_text(source)
        if kind == "ooxml" and subtype == "docx":
            return docx_to_text(source)
        if kind == "ole" and subtype == "doc":
            return doc_to_text(source)
        if kind == "rtf":
            return doc_to_text(source)
        if kind == "image":
            text, engine = _ocr_image(source)
            if text:
                return TextDoc(text=text, fidelity="ocr", kind="E_ocr", notes=[f"{engine} OCR"])
            return TextDoc(notes=["图片附件需 OCR，未安装 OCR 引擎"], fidelity="ocr-required", kind="E_ocr")
        if kind == "html":
            html = source.read_text("utf-8", errors="ignore")
            if "charset=gb" in html[:1000].lower():
                html = source.read_text("gb18030", errors="ignore")
            extraction = extract_html_text(html)
            return TextDoc(text=extraction.text or extraction.body_text, fidelity="text-layer", kind="A_html")
        if kind == "text":
            raw = source.read_bytes()
            for encoding in ("utf-8", "gb18030"):
                try:
                    return TextDoc(text=raw.decode(encoding), fidelity="text-layer", kind="text")
                except UnicodeDecodeError:
                    continue
        return TextDoc(notes=[f"未识别的附件类型：{kind}/{subtype}"], fidelity="unsupported")
    except Exception as exc:
        return TextDoc(notes=[f"附件解析失败：{exc}"], fidelity="unsupported")


def sniff_attachment_kind(path: str | Path) -> str:
    kind, subtype = _sniff(Path(path))
    return f"{kind}/{subtype}" if subtype else kind
