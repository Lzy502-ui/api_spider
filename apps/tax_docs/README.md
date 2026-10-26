# 各省税务文书采集

把客户提供的「各省税务文书.zip」报告中的原文链接，按三类文书规则分类、采集、
解析并暂存；同时可以生成既有 SQL Server `taxUrlCollect` 表的分类种子 SQL。

## 分类规则

| 报告中的文书类型 | 入库参数 | 目标文书类型 | `taxUrlCollect.Category` |
| --- | --- | --- | --- |
| 处理决定书 | 勾选「税务执法」「法律库隐藏」 | 监管措施/纪律处罚 | 1 |
| 处罚事项告知书 | 不勾选上述两项 | 行政处罚 | 2 |
| 处罚决定书 | 不勾选上述两项，按案号去重 | 行政处罚 | 3 |

一个公告同时包含「处理决定书 + 处罚决定书」时，本应用会拆成两条记录，
分别套用 Category 1 和 Category 3，而不是整页只取一种分类。

## 数据流

```text
各省税务文书.zip
  └─ 报告表格 → sources（日期/文号/类型/受送达人/URL）
       └─ 详情页抓取（带缓存、限速、重试）
            ├─ HTML 正文容器（id=zoom/mainText/js_content 或 class=TRS_Editor）
            ├─ HTML 附件（doc/docx/pdf/图片）
            ├─ Word（docx 直接解 OOXML，保留自动编号与表格）
            ├─ 原生 PDF（文本层判定）
            └─ 扫描件 PDF/图片（RapidOCR/Tesseract，可选）
                 └─ 规则化字段抽取 → documents
                      ├─ JSONL / CSV（复核、离线加工）
                      └─ taxUrlCollect SQL / 直连 SQL Server
```

## 快速开始

```bash
# 1. 解析报告，写入暂存库
python3 apps/tax_docs/cli.py import-reports --source 各省税务文书.zip

# 2. 试采 10 页
python3 apps/tax_docs/cli.py collect --limit 10

# 3. 查看统计
python3 apps/tax_docs/cli.py stats

# 4. 导出 JSONL / CSV / SQL / 统计
python3 apps/tax_docs/cli.py export --out-dir data/tax_docs/export
```

默认暂存库为 `data/tax_docs/tax_docs.db`。`data/` 已在 `.gitignore` 中。

## 命令

### import-reports

```bash
python3 apps/tax_docs/cli.py import-reports \
  --source /path/各省税务文书.zip \
  --db data/tax_docs/tax_docs.db
```

支持 `.zip` 或已解压目录。报告表格中同一 URL 的多行、合并单元格、无「文书类型」列
（按章节标题推断）都会处理。

### collect

```bash
python3 apps/tax_docs/cli.py collect \
  --limit 20 \
  --province 广东 \
  --delay 2 \
  --timeout 30 \
  --retries 2 \
  --known-numbers-file existing_numbers.txt
```

- 默认按来源主机限速 2 秒；原始响应缓存在 `data/tax_docs/cache/`，重跑不会重复请求。
- 被 403/412 拦截、网络失败、正文为空会分别记录为 `blocked`/`failed`/`empty_content`。
- `--retry` 会把失败来源重置为 `pending`。
- `--refresh` 忽略 HTTP 缓存。
- 处罚决定书默认按暂存库里已采集的案号去重；外部库已有案号可通过
  `--known-numbers-file` 传入（一行一个）。
- 公告正文里「一户一行」的表格会拆成多份文书，分别记录受送达人与案号。

### export

```bash
python3 apps/tax_docs/cli.py export --out-dir data/tax_docs/export --formats jsonl,csv,sql,md
```

- `tax_documents.jsonl`：完整字段 + 分类参数 + 保真度，适合复核和二次加工；
- `tax_documents.csv`：便于 Excel 抽查（不含正文）；
- `tax_url_collect.sql`：SQL Server `taxUrlCollect` 种子，含 `IF NOT EXISTS` 保护；
- `mixed_urls.csv`：同一 URL 同时包含多类文书的清单，交给本应用拆分采集或人工处理；
- `summary.md`：分类、状态、保真度统计。

一个 URL 如果同时包含多类文书，`taxUrlCollect.Category` 无法表达，默认跳过并计入
mixed（导出为 `mixed_urls.csv`）。这类页面不能用一次 Category 正确入库：本应用会按
文书类型拆分后单独采集；`--include-mixed` 仅用于人工确认后的强制播种（按最小 Category 写入一次）。

### apply-urls

```bash
export MSSQL_HOST=...
export MSSQL_PORT=1433
export MSSQL_USER=...
export MSSQL_PASSWORD=...
export MSSQL_DATABASE=...

python3 apps/tax_docs/cli.py apply-urls --apply
```

只写 `taxUrlCollect(url, flag=0, Category)`，不直接写加工库。`Category` 直接由报告中的
文书类型计算，所以 `import-reports` 之后即可执行，不需要先跑详情采集。后续仍由客户环境中的
`api_spider.py` 完成详情采集、AI 清洗、`ap_adminPenaltyCase`、`z_行政处罚采集` 等入库。
默认 dry-run。

> 不要再用 `python api_spider.py <zip>` 直接导入本批报告：原导入函数只写 `(url, flag)`，
> 不带 `Category`，所有 URL 都会落入 Category=0 而被跳过。请先用本应用播种，再运行
> `api_spider.py`（不传 zip 路径）。

## 可选依赖

核心程序只依赖 Python 标准库。以下能力按需安装：

```bash
pip install -r apps/tax_docs/requirements-optional.txt
```

| 能力 | 依赖 | 缺失时的行为 |
| --- | --- | --- |
| PDF 文本层 | `PyMuPDF` 或 `pdfplumber` | 记录 `unsupported` |
| 扫描件 OCR | `rapidocr-onnxruntime` 或 `pytesseract` + `Pillow` | 记录 `ocr-required`，待人工复核 |
| `.doc` 转换 | LibreOffice (`soffice`) 或 `antiword` | 记录 `unsupported` |
| 写 SQL Server | `pymssql` | 只能导出 SQL 人工执行 |

`.docx` 不依赖 `python-docx`：直接读取 OOXML，并解析 `numbering.xml` 还原「一、」
「（一）」等自动编号，避免正文编号丢失。页眉页脚不读取。

## 质量标记

每条文书记录带 `fidelity`：

- `text-layer`：网页或原生 PDF 文本层；
- `docx`：Word 结构解析；
- `ocr`：OCR 结果；
- `ocr-required`：需要 OCR 但当前环境没有引擎；
- `unsupported`：当前环境无法解析该格式。

`needs_review=1` 表示 OCR、缺关键字段、无案号、类型无法判断等，需要人工复核。

## 测试

```bash
python3 -m unittest discover -s apps/tax_docs/tests -v
```

测试覆盖分类规则、报告表格解析、案号拆分/去重、字段抽取、HTML 正文定位、
附件识别、受送达人表格拆分、暂存与导出。
