import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.tax_docs.recipient_table import extract_recipient_rows  # noqa: E402


HTML = """
<table>
  <tr><th>序号</th><th>纳税人名称</th><th>纳税人识别号</th><th>文书字轨号</th></tr>
  <tr><td>1</td><td>甲公司</td><td>91110000X</td><td>京税稽处〔2025〕1号</td></tr>
  <tr><td>2</td><td>乙公司</td><td>91110000Y</td><td>京税稽罚〔2025〕2号</td></tr>
</table>
"""

NO_TABLE = "<p>正文没有表格</p>"


class RecipientTableTest(unittest.TestCase):
    def test_extract_rows(self):
        rows = extract_recipient_rows(HTML)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["party"], "甲公司")
        self.assertEqual(rows[0]["numbers"], ["京税稽处〔2025〕1号"])
        self.assertEqual(rows[1]["numbers"], ["京税稽罚〔2025〕2号"])

    def test_no_table(self):
        self.assertEqual(extract_recipient_rows(NO_TABLE), [])


if __name__ == "__main__":
    unittest.main()
