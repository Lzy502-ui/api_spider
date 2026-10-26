import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.tax_docs.html_text import detect_attachment_urls, extract_html_text  # noqa: E402


LONG_TEXT = "为依法查处税收违法行为，现将有关情况公告如下。" * 20

CONTAINER_HTML = f"""
<html><head>
<meta name="ArticleTitle" content="关于送达《税务行政处罚决定书》的公告">
<meta name="PubDate" content="2024-05-14 17:20:44">
<meta name="ContentSource" content="国家税务总局惠东县税务局">
</head><body>
<nav>首页 信息公开 通知公告</nav>
<div class="nav">导航噪声</div>
<div id="zoom">
  <p><strong>国家税务总局惠东县税务局吉隆税务分局</strong></p>
  <p>税务行政处罚决定书</p>
  <p>惠东吉隆税罚（2024）01号</p>
  <p>{LONG_TEXT}</p>
  <p>附件：<a href="/files/a.pdf">税务行政处罚决定书.pdf</a></p>
</div>
</body></html>
"""

SHORT_HTML = """
<html><body>
<div class="content">
  <p>关于送达《税务行政处罚决定书》的公告</p>
  <p>发布时间：2024-05-14</p>
  <p>附件：<a href="files/a.pdf">决定书.pdf</a></p>
</div>
</body></html>
"""


class HtmlTextTest(unittest.TestCase):
    def test_container_and_meta(self):
        result = extract_html_text(CONTAINER_HTML)
        self.assertEqual(result.container, "#zoom")
        self.assertEqual(result.meta["ArticleTitle"], "关于送达《税务行政处罚决定书》的公告")
        self.assertIn("惠东吉隆税罚（2024）01号", result.text)
        self.assertIn("附件", result.text)
        self.assertNotIn("导航噪声", result.text)
        self.assertEqual(result.page_kind, "A_html")

    def test_short_page(self):
        result = extract_html_text(SHORT_HTML)
        self.assertLess(len(result.text), 300)
        self.assertEqual(result.page_kind, "B_html_attachment")
        links = detect_attachment_urls(SHORT_HTML, "https://example.com/notice.html")
        self.assertEqual(links[0]["url"], "https://example.com/files/a.pdf")

    def test_attachment_detection_dedup(self):
        html = """
        <a href="/a.pdf">附件</a>
        <a href="/a.pdf">重复</a>
        <a href="/page.html">普通链接</a>
        """
        links = detect_attachment_urls(html, "https://example.com/x/")
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["url"], "https://example.com/a.pdf")


if __name__ == "__main__":
    unittest.main()
