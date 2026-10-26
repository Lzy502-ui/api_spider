import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.tax_docs.fields import (  # noqa: E402
    classify_party,
    extract_fields,
    extract_law_according,
    normalize_date,
)


SAMPLE = """国家税务总局北京市税务局第三稽查局
税务处理决定书
京税稽三处〔2024〕296号
北京勇英杰业商贸有限公司：
经查，你单位存在以下违法事实：
一、未按规定期限申报纳税。
根据《中华人民共和国税收征收管理法》第三十二条、《中华人民共和国税收征收管理法实施细则》第七十五条之规定，决定追缴税款。
如不服本决定，可依法申请行政复议。
国家税务总局北京市税务局第三稽查局
2024年5月29日
"""


class FieldsTest(unittest.TestCase):
    def test_dates(self):
        self.assertEqual(normalize_date("2024年5月29日"), "2024-05-29")
        self.assertEqual(normalize_date("二〇二四年五月二十九日"), "2024-05-29")
        self.assertEqual(normalize_date("2024/05/29"), "2024-05-29")
        self.assertEqual(normalize_date("not a date"), "")

    def test_extract_sample(self):
        fields = extract_fields(SAMPLE)
        self.assertEqual(fields.number, "京税稽三处〔2024〕296号")
        self.assertEqual(fields.agency, "国家税务总局北京市税务局第三稽查局")
        self.assertEqual(fields.penalty_date, "2024-05-29")
        self.assertEqual(fields.party, "北京勇英杰业商贸有限公司")
        self.assertEqual(fields.party_class, "法人及其他组织")
        self.assertIn("中华人民共和国税收征收管理法", fields.law_according)
        self.assertIn("第三十二条", fields.law_according)
        self.assertIn("未按规定期限申报纳税", fields.body)
        self.assertNotIn("京税稽三处〔2024〕296号", fields.body)

    def test_law_groups(self):
        text = "依据《税收征收管理法》第三十二条、第六十四条和《行政处罚法》第二十八条之规定。"
        law = extract_law_according(text)
        self.assertIn("《税收征收管理法》第三十二条、第六十四条", law)
        self.assertIn("《行政处罚法》第二十八条", law)

    def test_party_class(self):
        self.assertEqual(classify_party("北京某某商贸有限公司"), "法人及其他组织")
        self.assertEqual(classify_party("张三"), "个人")
        self.assertEqual(classify_party("上海某某商贸有限公司等6户单位"), "法人及其他组织")

    def test_missing_fields_are_reported(self):
        fields = extract_fields("只有一句话。")
        self.assertIn("adminPenaltyNumber", fields.missing)
        self.assertIn("trialCourtNameOld", fields.missing)


if __name__ == "__main__":
    unittest.main()
