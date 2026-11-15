# 定义契约 freightrate

## 数值

| 列 | 类型 | 量化 | 可空 | 说明 |
|---|---|---|---|---|
| weight_kg | Decimal | 0.001 kg | 否 | 实重，输入单位 g/kg/t 都换算到 kg |
| length_cm / width_cm / height_cm | Decimal | 0.001 cm | 否 | 尺寸，输入单位 cm/m/mm 都换算到 cm |
| volumetric_kg | Decimal | 0.001 kg | 否 | 长×宽×高 ÷ volume_divisor |
| billable_kg | Decimal | 0.5 kg 向上 | 否 | 取实重与体积重的较大值后按台阶进位 |
| first_price / step_price / min_charge | Decimal | 0.01 元 | 否 | 运价表金额 |
| fuel_pct / insurance_pct | Decimal | 小数 | 否 | 8.5% 与 0.085 两种写法都归到小数 |
| declared_value | Decimal | 0.01 元 | 是 | 空值与哨兵都按 0 处理，表示未保价 |
| total_amount | Decimal | 0.01 元 | 否 | 由分项求和得出，不单独舍入 |

金额与用量一律 Decimal，对外 JSON 用字符串。禁止 float 累加。

## 单位

单位表只在 `app/config.py` 定义一份。重量 g=0.001 kg、t=1000 kg；尺寸 m=100 cm、mm=0.1 cm。
带单位后缀的单元格（如 3200g）先剥后缀，单位列非空时以单位列为准。

## 时间

- 存储：UTC，ISO 文本，秒精度，带 Z 后缀
- 输入：接受 Z、+hh:mm、无偏移三种写法；无偏移按 +08:00 解释
- 展示：页面显示 Asia/Shanghai，生成时间与批次时间都带时区名
- 运价版本窗口：`effective_from` 与 `effective_to` 也是 UTC，区间半开 [from, to)

## 空值

- `null` / 空串 / 哨兵 三者语义分开
- 哨兵清单：`-`、`--`、`/`、`N/A`、`NA`、`NULL`、`NONE`、`未知`、`无`、`空`
- 重量与尺寸出现哨兵即进隔离区；声明价值出现哨兵按未保价处理

## 半开区间

运价版本用 `[effective_from, effective_to)`：提货时刻等于 from 命中该版，等于 to 落到下一版。
相邻版本首尾相接时不重不漏。

## 去重键

业务键是运单号 `waybill_no`。同一运单号第二次出现时比较载荷
（承运商、服务、分区、提货时刻、重量、三边尺寸、声明价值）：
载荷相同记 DUPLICATE_WAYBILL，不同记 DUP_CONFLICT，两种都只保留第一条参与计费。

## 空态

- 0 字节文件：拒绝采信，返回 EMPTY_FILE
- 只有表头：采信成功但 rows_in 为 0，页面显示空态
- 全是空行：empty_rows 计数，rows_in 为 0

## 原因码

原因码只在 `app/reasons.py` 定义，分两级：`block` 表示该行不进计费，`info` 表示只做标记。
每条隔离都带原始行号与原因码，可导出 CSV 复核。

## 月结

### 关账期与范围

- 关账期 `period`：`YYYY-MM`，提货时刻按 `Asia/Shanghai` 落月
- 本期运单：来源中提货时刻落在本期的行；同号以更晚登记的补发为准，旧值进「被补发覆盖」证据
- 超范围行（`PERIOD_OUT_OF_SCOPE`）：来源里提货时刻不在本期的行，只进证据，不计费
- 运价合并：同一承运商以最后登记它的来源为准，其它承运商沿用先前来源

### 版本状态

| 状态 | 含义 | 可否导出 | 可否再发布 |
|---|---|---|---|
| `draft` | 已定稿待复核 | 可按 version_id 预览导出 | 复核后可发布 |
| `published` | 当前正式版 | 是（正式导出） | 重复发布为幂等空操作 |
| `superseded` | 被后续正式版取代 | 是（按 version_id 整份保留） | 否，返回 CLOSE_SUPERSEDED |

同一期任意时刻至多一个 `published` 版本，由 `close_official` 指针唯一确定。

### 指纹

- `basis_digest`：规则版本 + 参数档位 + 每家承运商取自哪个运价来源 + 合并后运价卡 +
  当选运单及来源 + 被覆盖/超范围/隔离事件。相同即同一版本，重复构建不新增。
- `impact_digest`：逐票账单差异（旧新计费重、版本、应收、分项差额）+ 三类范围事件。
  复核记录携带该指纹，发布时校验，不符即要求重新复核。
- `rate_digest`：合并后运价卡的规范化摘要，用于比对两版是否换了价。

### 证据行类型（`row_kind`）

| 类型 | 含义 |
|---|---|
| `invoice` | 本期计费票，含计费重依据、选用运价与分项 |
| `unbilled` | 本期运单但运价无命中或重叠，未计费 |
| `quarantine` | 采信阶段被隔离的源行 |
| `excluded` | 提货时刻不在本期，未参与计费 |
| `superseded` | 同号被更晚补发覆盖的旧值 |
