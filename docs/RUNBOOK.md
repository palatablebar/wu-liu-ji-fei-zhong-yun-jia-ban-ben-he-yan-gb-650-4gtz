# 运行手册 freightrate

## 启动

```bash
docker compose config --quiet
docker compose up -d --build
curl -s http://127.0.0.1:18209/api/health
```

宿主机端口 18209 映射容器 8000。SQLite 落在命名卷 `freightrate-data` 的 `/app/data/freightrate.db`。

## 启动后自动完成的事

1. 建表
2. 装载 `sample_data/rate_cards.csv`（运价集 v3，38 行）
3. 采信 `sample_data/waybills_dirty.csv`
4. 计算计费重与账单

页面首次打开即可看到 27 条运单、15 票计费、12 行隔离、应收 1521.04 元。

## 手工重跑

```bash
curl -s -X POST http://127.0.0.1:18209/api/waybills/import -F sample=waybills_dirty.csv
curl -s -X POST http://127.0.0.1:18209/api/weights
curl -s -X POST http://127.0.0.1:18209/api/charges
curl -s http://127.0.0.1:18209/api/reconcile
```

同一份输入重复导入命中同一个 run_id，行数与合计不变。

## 运价文件校验

```bash
curl -s -X POST http://127.0.0.1:18209/api/rates/audit -F sample=rate_cards_overlap.csv
```

返回重叠清单；`/api/rates/import` 在校验不通过时返回 409，不会替换现有运价。

## 月结出账

关账前承运商常补来历史运价修正和迟到运单。不要等齐，也不要直接改已发出的账单，
按下面的顺序出账：

```bash
P=2026-04
# 1. 登记本期运价（可多份，按承运商合并；重叠/陈旧/缺列一律拒绝）
curl -s -X POST http://127.0.0.1:18209/api/close/rates -F period=$P -F sample=rate_cards.csv
# 2. 登记本期运单（同一份文件只登记一次，重复提交不改范围）
curl -s -X POST http://127.0.0.1:18209/api/close/waybills -F period=$P -F sample=waybills_dirty.csv
# 3. 生成待复核版本（相同范围返回同一版本，不新增）
curl -s -X POST http://127.0.0.1:18209/api/close/build -F period=$P
# 4. 看补发影响：改了哪些票、旧新计费重与应收、差额
curl -s "http://127.0.0.1:18209/api/close/versions/CLOSE-$P-v1/impact"
# 5. 登记复核并发布（复核绑定影响指纹）
curl -s -X POST "http://127.0.0.1:18209/api/close/versions/CLOSE-$P-v1/review" \
  -F reviewer=复核人 -F note=四账已核
curl -s -X POST "http://127.0.0.1:18209/api/close/versions/CLOSE-$P-v1/publish"
# 6. 取正式版导出（只认正式指针）
curl -s "http://127.0.0.1:18209/api/close/official/invoice.csv?period=$P" -o invoice.csv
curl -s "http://127.0.0.1:18209/api/close/official/evidence.csv?period=$P" -o evidence.csv
curl -s "http://127.0.0.1:18209/api/close/official/manifest.json?period=$P" -o manifest.json
```

补发在关账前又到一批时，重复 1-5：旧正式版自动转为 superseded 并整份保留，新版本
只有在复核后才成为正式；期间正式导出一直指向旧版，不会出现半新半旧。

失败处理：

| 现象 | 原因 | 处理 |
|---|---|---|
| 409 CLOSE_REVIEW_REQUIRED | 还没登记复核 | 先 `review` 再 `publish` |
| 409 CLOSE_REVIEW_STALE | 复核后影响又变了 | 对新版本重新复核 |
| 409 RATE_OVERLAP / RATE_STALE | 补发运价重叠或版本更低 | 修正文件后重提，现有范围不变 |
| 409 CLOSE_NO_RATES / CLOSE_NO_SOURCES | 本期缺运价或缺运单 | 先登记对应来源 |
| 409 CLOSE_SUPERSEDED | 试图发布已被取代的版本 | 发布当前最新草稿 |
| 409 NO_OFFICIAL_VERSION | 本期还没发布过 | 先走完复核与发布 |

## 重建数据库

```bash
docker compose down -v --remove-orphans
docker compose up -d --build
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面 500，提示 RATE_MISSING | 采信时运价表还没装载 | 先打开运价手册页确认 38 行版本窗口 |
| /api/charges 返回 409 NO_WEIGHTS | 还没算计费重 | 先调 /api/weights |
| 导入后 rows_ok 为 0 | 全部行都被隔离 | 到运单页看原因码分布 |
| 运价装载 409 RATE_STALE | 提交的运价集版本低于现有 | 提高 CSV 里的 rate_set_version |
| 月结页面看不到新草稿 | 默认展示待复核的最新草稿 | 用 `/close?period=2026-04&version=…` 指定版本 |

## 数据表

- `runs` / `waybills` / `quarantine` / `weight_lines` / `charge_lines`：M1-M5 的预览运行数据，可重算
- `close_*`：月结正式数据。`close_official` 只有一行指针（每期一行），正式导出只认它；
  版本、账单、证据、四账、影响均为按 `version_id` 分区的不可变快照
