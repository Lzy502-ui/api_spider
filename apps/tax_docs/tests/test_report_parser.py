import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.tax_docs.classify import DocType  # noqa: E402
from apps.tax_docs.report_parser import (  # noqa: E402
    parse_report_html,
    province_from_filename,
)


HTML = """
<html><body>
<div class="section-title">一、《税务处理决定书》</div>
<div class="district-card">
  <div class="district-header"><span>3. 石景山区税务局</span></div>
  <div class="district-body">
    <div class="district-meta"><span>发布机关：石景山区税务局古城税务所</span></div>
    <table>
      <thead><tr><th>序号</th><th>日期</th><th>文件号</th><th>文书类型</th>
      <th>受送达人</th><th>原文链接</th></tr></thead>
      <tbody>
        <tr><td>1</td><td class="date">2025-06-20</td>
        <td class="doc-num">京石古税罚告[2025]3号</td>
        <td><span class="doc-type">税务行政处罚事项告知书</span></td>
        <td>北京某某商贸有限公司</td>
        <td><a href="http://example.com/a.shtml">查看原文</a></td></tr>
        <tr><td>2</td><td>2025-06-21</td>
        <td>京石古税罚[2025]4号</td>
        <td>税务行政处罚决定书</td>
        <td>北京另一商贸有限公司</td>
        <td><a href="http://example.com/b.shtml">查看</a></td></tr>
      </tbody>
    </table>
  </div>
</div>
</body></html>
"""


class ReportParserTest(unittest.TestCase):
    def test_parse_two_rows(self):
        records = parse_report_html(HTML, "北京市15区税务局_税务文书筛选综合报告.html")
        self.assertEqual(len(records), 2)
        first, second = records
        self.assertEqual(first.province, "北京市15区税务局")
        self.assertEqual(first.publish_date, "2025-06-20")
        self.assertEqual(first.numbers, ["京石古税罚告[2025]3号"])
        self.assertEqual(first.doc_types, [DocType.NOTICE])
        self.assertEqual(first.agency, "石景山区税务局古城税务所")
        self.assertEqual(first.url, "http://example.com/a.shtml")
        self.assertEqual(second.doc_types, [DocType.PENALTY])
        self.assertEqual(second.url, "http://example.com/b.shtml")

    def test_infer_type_from_section_when_no_type_column(self):
        html = """
        <div class="section-title">三、《税务行政处罚决定书》</div>
        <table><thead><tr><th>序号</th><th>日期</th><th>文件号</th>
        <th>受送达人 / 案件名称</th><th>发布机关</th><th>原文链接</th></tr></thead>
        <tbody>
          <tr><td>1</td><td>2024-05-29</td><td>京税稽三处〔2024〕296号</td>
          <td>北京勇英杰业商贸有限公司</td><td>第三稽查局</td>
          <td><a href="http://example.com/c.shtml">查看原文</a></td></tr>
        </tbody></table>
        """
        records = parse_report_html(html, "北京市税务局_税务文书筛选报告（西城区）.html")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].doc_types, [DocType.PENALTY])

    def test_missing_url(self):
        html = """
        <table><thead><tr><th>日期</th><th>文件号</th><th>文书类型</th>
        <th>受送达人</th><th>原文链接</th></tr></thead>
        <tbody><tr><td>2023-01-01</td><td>X税稽罚〔2023〕1号</td>
        <td>处罚决定书</td><td>某公司</td><td>URL未获取</td></tr></tbody></table>
        """
        records = parse_report_html(html, "深圳市_税务文书筛选综合报告.html")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "url_missing")
        self.assertEqual(records[0].url, "")

    def test_province_from_filename(self):
        self.assertEqual(province_from_filename("5个计划单列市税务局_报告.html"), "计划单列市税务局")
        self.assertEqual(province_from_filename("云南省17市州税务局_报告.html"), "云南省17市州税务局")


if __name__ == "__main__":
    unittest.main()
