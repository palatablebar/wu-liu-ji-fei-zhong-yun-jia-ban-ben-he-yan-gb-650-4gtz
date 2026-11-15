# 深度登记 freightrate 物流计费重与运价版本核验

- 规模：中型（8 个功能模块，74-80 文件区间）
- 核心规则：按提货时刻在运价版本半开区间内择版，计费重取实重与体积重的较大值再按台阶向上进位，分区价与附加费按固定顺序叠算，分项之和必须等于应收
- 使用者与工作流：3PL 结算组每天收 3 家承运商的运单与月度运价表，先采信运单，再逐票算计费重与应收，最后与承运商账单对账并导出

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 运单采信 | 导入脏运单，看到逐行诊断与原始行号 | POST /api/waybills/import | `app/modules/m1_waybill/handler.py` `app/modules/m1_waybill/service.py` `app/modules/m1_waybill/reader.py` `app/modules/m1_waybill/decode.py` `app/modules/m1_waybill/normalize.py` | `tests/test_m1_waybill.py` |
| M2 运价版本 | 看版本窗口、校验运价文件是否重叠或陈旧 | GET /api/rates | `app/modules/m2_rates/handler.py` `app/modules/m2_rates/service.py` `app/modules/m2_rates/ratecard.py` `app/modules/m2_rates/version.py` `app/modules/m2_rates/surcharge.py` | `tests/test_m2_rates.py` |
| M3 质量隔离 | 看原因码分布与隔离行，导出证据 CSV | GET /api/quarantine | `app/modules/m3_quality/handler.py` `app/modules/m3_quality/service.py` `app/modules/m3_quality/rules.py` `app/modules/m3_quality/quarantine.py` `app/modules/m3_quality/metrics.py` | `tests/test_m3_quality.py` |
| M4 计费重 | 逐票看实重、体积重、来源与进位后计费重 | POST /api/weights | `app/modules/m4_weights/handler.py` `app/modules/m4_weights/service.py` `app/modules/m4_weights/volumetric.py` `app/modules/m4_weights/rounding.py` `app/modules/m4_weights/billable.py` | `tests/test_m4_weights.py` |
| M5 分区附加 | 看首重、续重、偏远、燃油、保价与应收 | POST /api/charges | `app/modules/m5_charges/handler.py` `app/modules/m5_charges/service.py` `app/modules/m5_charges/zone.py` `app/modules/m5_charges/addon.py` `app/modules/m5_charges/money.py` | `tests/test_m5_charges.py` |
| M6 账单对账 | 看四账是否相等，差在哪一项 | GET /api/reconcile | `app/modules/m6_reconcile/handler.py` `app/modules/m6_reconcile/service.py` `app/modules/m6_reconcile/compare.py` `app/modules/m6_reconcile/totals.py` `app/modules/m6_reconcile/explain.py` | `tests/test_m6_reconcile.py` |
| M7 导出 | 导出账单、证据行与可复算的运行清单 | GET /api/export/invoice.csv | `app/modules/m7_export/handler.py` `app/modules/m7_export/service.py` `app/modules/m7_export/csv_out.py` `app/modules/m7_export/invoice_out.py` | `tests/test_m7_export.py` |
| M8 月结出账 | 定本期运价与运单范围、看补发影响、复核、原子切正式版 | GET /close | `app/modules/m8_close/handler.py` `app/modules/m8_close/service.py` `app/modules/m8_close/scope.py` `app/modules/m8_close/billing.py` `app/modules/m8_close/export.py` | `tests/test_m8_close.py` `tests/test_m8_close_http.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 运价与运单导出编码不同，GB18030 与 UTF-8 混用 | 直接按 UTF-8 解码抛异常，整份文件读不进 | 编码回退链 utf-8-sig → utf-8 → gb18030，命中回退给 ENCODING_FALLBACK | `app/modules/m1_waybill/decode.py` | `tests/test_m1_waybill.py` |
| TRAP-05 | 中段一行少一列 | 静默补 None，后续列错位算错分区 | 严格列数校验，行数与表头不等即隔离并给 ARITY_MISMATCH | `app/modules/m1_waybill/reader.py` | `tests/test_m1_waybill.py` |
| TRAP-06 | 重量写成 2,500 与全角 １２.５ | float 崩，或全角被当成 0 | NFKC 归一 + 去千分位 + 单位后缀剥离 | `app/modules/m1_waybill/normalize.py` | `tests/test_m1_waybill.py` |
| TRAP-07 | 重量写成 - 或 N/A | 当成 0 参与合计，运费凭空少一票 | 哨兵清单集中定义，三态分开，哨兵行进隔离区 | `app/modules/m3_quality/rules.py` | `tests/test_m3_quality.py` |
| TRAP-10 | 20 票各分项用 float 累加 | 合计差 0.01 到 0.10，对账永远不平 | 全程 Decimal，分项先量化到分再求和 | `app/modules/m5_charges/money.py` | `tests/test_m5_charges.py` |
| TRAP-11 | 提货时间一处写 Z 一处写 +08:00 | 同一时刻落进两个版本窗口 | 先归一到 UTC 再择版，写法混用标 TZ_MIXED | `app/modules/m1_waybill/normalize.py` | `tests/test_m2_rates.py` |
| TRAP-14 | 同一运单号出现两次，重量不同 | 后写覆盖先写，结果取决于文件顺序 | 保留首条，后条按载荷是否一致分成 DUP_CONFLICT 与 DUPLICATE_WAYBILL | `app/modules/m3_quality/rules.py` | `tests/test_m3_quality.py` |
| TRAP-15 | 重量用 g 与 kg，尺寸用 cm 与 m | 直接按数字算，体积重差 1000 倍 | 单位表集中定义，先换算到 kg 与 cm 再算 | `app/modules/m1_waybill/normalize.py` | `tests/test_m1_waybill.py` |
| TRAP-16 | 文件末尾是半行 | 半行被当成有效运单计费 | 末条列数不足记为 TRUNCATED_TAIL 并隔离 | `app/modules/m1_waybill/reader.py` | `tests/test_m1_waybill.py` |
| TRAP-23 | 提货时刻恰好等于版本生效起点或终点 | 两端都算，同一票落进两个版本 | 半开区间 [生效起, 生效止)，终点归到下一版 | `app/modules/m2_rates/version.py` | `tests/test_m2_rates.py` |
| TRAP-24 | 各分项各自四舍五入 | 分项之和与应收差一分钱 | 分项先量化到分，应收由分项求和得出 | `app/modules/m5_charges/money.py` | `tests/test_m5_charges.py` |
| TRAP-25 | 同分区两版生效区间相交 | 取到哪版看运气，同一票时高时低 | 先检测重叠，重叠即整份运价集拒绝装载 | `app/modules/m2_rates/version.py` | `tests/test_m2_rates.py` |
| TRAP-26 | 用旧运价集重新提交 | 旧价覆盖新价，已出账单金额回退 | 运价集版本号比较，低于现有版本直接拒绝 | `app/modules/m2_rates/service.py` | `tests/test_m2_rates.py` |
| TRAP-30 | 关账前承运商补来历史运价修正与迟到运单 | 等齐再出账错过关账；直接覆盖已发账单让同票计费重与应收悄悄变化 | 出账定稿为不可变版本：先定范围，再按影响复核，通过后原子切正式指针，旧版整份保留 | `app/modules/m8_close/service.py` `app/modules/m8_close/scope.py` | `tests/test_m8_close.py` |
| TRAP-31 | 同一份补发被反复重试提交 | 每提交一次就多一版账单，金额重复计入 | 来源按内容摘要去重，版本按范围指纹去重；重试返回同一版本 | `app/modules/m8_close/service.py` `app/store.py` | `tests/test_m8_close.py` |
| TRAP-32 | 某家承运商只补自己那半张运价表 | 整份替换把别家价格冲掉，别家账单凭空变化 | 运价按承运商合并，后登记的补发只替换它覆盖到的承运商 | `app/modules/m8_close/scope.py` | `tests/test_m8_close.py` |
| TRAP-33 | 发布过程中进程中断 | 正式指针已挪、账单行没写完，导出缺行 | 版本快照与指针切换各在一个事务，中断整份回滚，旧正式版原样可用 | `app/store.py` | `tests/test_m8_close.py` |
| TRAP-34 | 复核人看过的影响在发布前又变了 | 复核结论套用到另一份账单上 | 复核记录绑定影响指纹，指纹不符则拒绝发布并要求重新复核 | `app/modules/m8_close/service.py` | `tests/test_m8_close.py` |

## 对账规则

- 运单行账：采信记录 = 计费 + 隔离（有未计费行时单列第三项）
- 金额账：应收合计 = 基础运费 + 续重运费 + 偏远附加 + 燃油附加 + 保价费（Decimal，分）
- 版本账：可计费运单 = 单选命中 + 无命中或重叠
- 计费重账：计费行数 = 实重择用 + 体积重择用
- 重跑：同一份输入得到同一个 run_id，行数与合计不变

## 月结口径

月结四账按「本期运单行」算，与上面对账页的运行口径不同，两者都自洽：

- 运单行账：本期运单行 = 计费 + 未计费 + 被补发覆盖。隔离行从未进采信表、超范围行
  属于别的月份，两者单列在版本清单与证据里，不混进本期行账。
- 金额账：应收合计 = 五个分项之和，口径与 M5/M6 完全一致。
- 版本账：本期运单 = 单选命中 + 无命中或重叠。
- 计费重账：计费行数 = 实重择用 + 体积重择用。
- 范围指纹：本期规则版本、合并后运价卡、当选运单及其来源、被覆盖/超范围/隔离事件。
  指纹相同即同一版本，不新增。
- 影响指纹：账单逐票差异 + 三类范围事件。复核绑定影响指纹，指纹变化即复核失效。

## 已知不覆盖

- 不做承运商账单文件的二次导入对账，本项目只核验自家计费结果是否四账相等
- 不处理多币种换算，运价表 currency 固定 CNY，只做展示
- 体积重只支持单一除数，不支持按件拆分后逐件取整
- 不支持按票拆分多段运输（一票多分区），分区按整票一个值处理
- 月结不支持重开已关账的历史月；已发布版本只读，如需更正应在下一期补差或新开一期
- 月结不做承运商对账文件自动比对，补发影响只描述自家账单的变化
