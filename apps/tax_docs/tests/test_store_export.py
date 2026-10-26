import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.tax_docs.classify import DocType, SourceRecord, source_record_key  # noqa: E402
from apps.tax_docs.export import export_jsonl, export_tax_url_sql  # noqa: E402
from apps.tax_docs.store import connect, iter_sources, stats, upsert_document, upsert_sources  # noqa: E402


def make_source(url: str = "https://example.com/a.shtml", raw_types: str = "处理决定书") -> SourceRecord:
    number = "京税稽处〔2024〕1号"
    return SourceRecord(
        record_key=source_record_key("北京市", url, number, raw_types, 1),
        report="report.html",
        province="北京市",
        row_no=1,
        publish_date="2024-05-14",
        raw_types=raw_types,
        numbers=[number],
        party="北京某某商贸有限公司",
        agency="国家税务总局北京市税务局稽查局",
        url=url,
    )


class StoreExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "tax.db"
        self.connection = connect(self.db)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_source_roundtrip(self):
        source = make_source()
        upsert_sources(self.connection, [source])
        rows = iter_sources(self.connection)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_types"], "处理决定书")
        self.assertEqual(stats(self.connection)["total_sources"], 1)

    def test_document_and_export(self):
        source = make_source()
        upsert_sources(self.connection, [source])
        upsert_document(
            self.connection,
            {
                "record_key": source.record_key + ":handling",
                "source_key": source.record_key,
                "source_url": source.url,
                "doc_type": DocType.HANDLING.value,
                "category": 1,
                "tax_enforcement": 1,
                "law_library_hidden": 1,
                "document_type_label": "监管措施/纪律处罚",
                "dedup_by_case_number": 0,
                "title": "税务处理决定书",
                "number": "京税稽处〔2024〕1号",
                "numbers_json": ["京税稽处〔2024〕1号"],
                "status": "collected",
            },
        )
        output = Path(self.temp.name) / "out"
        self.assertEqual(export_jsonl(self.connection, output / "docs.jsonl"), 1)
        record = json.loads((output / "docs.jsonl").read_text("utf-8").splitlines()[0])
        self.assertEqual(record["category"], 1)
        self.assertTrue(record["law_library_hidden"])

        written, mixed = export_tax_url_sql(self.connection, output / "urls.sql")
        self.assertEqual(written, 1)
        self.assertEqual(mixed, 0)
        sql = (output / "urls.sql").read_text("utf-8")
        self.assertIn("INSERT INTO taxUrlCollect", sql)
        self.assertIn("Category) VALUES (N'https://example.com/a.shtml', 0, 1)", sql)

    def test_mixed_category_is_skipped(self):
        source = make_source(raw_types="处理决定书+处罚决定书")
        upsert_sources(self.connection, [source])
        for doc_type, category in ((DocType.HANDLING, 1), (DocType.PENALTY, 3)):
            upsert_document(
                self.connection,
                {
                    "record_key": source.record_key + ":" + doc_type.value,
                    "source_key": source.record_key,
                    "source_url": source.url,
                    "doc_type": doc_type.value,
                    "category": category,
                    "status": "collected",
                },
            )
        output = Path(self.temp.name) / "out"
        written, mixed = export_tax_url_sql(self.connection, output / "urls.sql")
        self.assertEqual(written, 0)
        self.assertEqual(mixed, 1)


if __name__ == "__main__":
    unittest.main()
