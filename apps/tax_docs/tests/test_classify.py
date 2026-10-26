import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.tax_docs.classify import (  # noqa: E402
    CATEGORY_DEDUP,
    CATEGORY_EXTRA_TABLES,
    CATEGORY_NORMAL,
    DocType,
    classify_doc_type,
    classify_raw_types,
    dedup_decision,
    split_case_numbers,
)


class ClassificationTest(unittest.TestCase):
    def test_single_types(self):
        handling = classify_doc_type(DocType.HANDLING)
        self.assertEqual(handling.category, CATEGORY_EXTRA_TABLES)
        self.assertTrue(handling.tax_enforcement)
        self.assertTrue(handling.law_library_hidden)
        self.assertEqual(handling.document_type_label, "监管措施/纪律处罚")

        notice = classify_doc_type(DocType.NOTICE)
        self.assertEqual(notice.category, CATEGORY_NORMAL)
        self.assertFalse(notice.tax_enforcement)
        self.assertFalse(notice.law_library_hidden)
        self.assertEqual(notice.document_type_label, "行政处罚")

        penalty = classify_doc_type(DocType.PENALTY)
        self.assertEqual(penalty.category, CATEGORY_DEDUP)
        self.assertTrue(penalty.dedup_by_case_number)
        self.assertFalse(penalty.tax_enforcement)
        self.assertFalse(penalty.law_library_hidden)

    def test_aliases(self):
        self.assertEqual(classify_raw_types("税务处理决定书"), [DocType.HANDLING])
        self.assertEqual(classify_raw_types("税务行政处罚事项告知书"), [DocType.NOTICE])
        self.assertEqual(classify_raw_types("税务行政处罚决定书（简易）"), [DocType.PENALTY])
        self.assertEqual(classify_raw_types("处罚决定书（简易）"), [DocType.PENALTY])

    def test_mixed_types(self):
        self.assertEqual(
            classify_raw_types("处理决定书+处罚决定书"),
            [DocType.HANDLING, DocType.PENALTY],
        )
        self.assertEqual(
            classify_raw_types("处理决定书 处罚决定书"),
            [DocType.HANDLING, DocType.PENALTY],
        )
        self.assertEqual(
            classify_raw_types("处理决定书+不予处罚决定书"),
            [DocType.HANDLING, DocType.SKIP],
        )
        self.assertEqual(
            classify_raw_types("处罚决定书+处罚事项告知书"),
            [DocType.PENALTY, DocType.NOTICE],
        )

    def test_unknown(self):
        self.assertEqual(classify_raw_types("待确认"), [DocType.UNKNOWN])
        self.assertEqual(classify_raw_types(""), [DocType.UNKNOWN])
        self.assertEqual(classify_raw_types("不予处罚决定书"), [DocType.SKIP])

    def test_case_numbers(self):
        numbers = split_case_numbers("京石古税罚告[2025]3号、京房城税简罚〔2025〕19号")
        self.assertEqual(numbers, ["京石古税罚告[2025]3号", "京房城税简罚〔2025〕19号"])
        self.assertEqual(split_case_numbers("附件清单（6户）"), [])

    def test_dedup(self):
        self.assertEqual(dedup_decision([], set()), "no_number")
        self.assertEqual(dedup_decision(["京税稽罚〔2024〕1号"], set()), "new")
        self.assertEqual(
            dedup_decision(["京税稽罚〔2024〕1号"], {"京税稽罚[2024]1号"}),
            "all_duplicate",
        )
        self.assertEqual(
            dedup_decision(
                ["京税稽罚〔2024〕1号", "京税稽罚〔2024〕2号"],
                {"京税稽罚[2024]1号"},
            ),
            "partial_duplicate",
        )


if __name__ == "__main__":
    unittest.main()
