# 批量采集-解析

各省税务文书报告解析、原文链接采集与分类入库程序。

程序位于 [`apps/tax_docs/`](apps/tax_docs/)，详见该目录的 [README](apps/tax_docs/README.md)。

## 功能

- 解析「各省税务文书.zip」中的筛选报告，提取日期、文号、文书类型、受送达人、原文 URL；
- 按三类文书规则映射 `taxUrlCollect.Category`：

  | 报告中的文书类型 | 入库参数 | 目标文书类型 | `Category` |
  | --- | --- | --- | --- |
  | 处理决定书 | 勾选「税务执法」「法律库隐藏」 | 监管措施/纪律处罚 | 1 |
  | 处罚事项告知书 | 不勾选 | 行政处罚 | 2 |
  | 处罚决定书 | 不勾选，按案号去重 | 行政处罚 | 3 |

- 采集详情页：HTML 正文、网页附件（doc/docx/pdf/图片）、Word、原生 PDF、扫描件 OCR；
- 导出 JSONL / CSV / `taxUrlCollect` SQL，或直连 SQL Server 播种（默认 dry-run）。

## 快速开始

```bash
# 解析报告，写入暂存库
python3 apps/tax_docs/cli.py import-reports --source 各省税务文书.zip

# 试采 10 页
python3 apps/tax_docs/cli.py collect --limit 10

# 查看统计
python3 apps/tax_docs/cli.py stats

# 导出 JSONL / CSV / SQL / 统计
python3 apps/tax_docs/cli.py export --out-dir data/tax_docs/export
```

核心程序只依赖 Python 标准库；PDF、OCR、`.doc` 转换、SQL Server 写入的可选依赖见
[`apps/tax_docs/requirements-optional.txt`](apps/tax_docs/requirements-optional.txt)。

## 测试

```bash
python3 -m unittest discover -s apps/tax_docs/tests -v
```
