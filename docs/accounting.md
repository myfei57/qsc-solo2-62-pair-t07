# 分项冷负荷核算口径

入库旺季每小时几百批登记，旧算法只用“温差 × 固定系数”一个即时值，既无法随时间累计，
又漏算化霜热。本子系统把冷负荷拆成三个分项、分别按时间积分，并把登记凭证与核算结果
钉在同一口径上。

## 1. 分项

| 分量 | 键名 | 物理模型 | 触发方式 |
| --- | --- | --- | --- |
| 入库热 | `inbound` | `m·cp·(T货−T库)/3600`（kWh，负值截断为 0） | 批次登记（脉冲） |
| 围护热 | `envelope` | `U·A·(T环境−T库)`（kW，负值截断为 0），相邻采样间线性功率梯形积分 | 环境温度采样 |
| 化霜热 | `defrost` | `P化霜·时长/3600`（kWh），区间积分 | 化霜开始/结束区间 |

三项分别累计到整点小时桶（`hour = unix // 3600`），区间/采样跨整点时按梯形在桶界切分。

## 2. 已明确的口径选择（只挑一种）

1. **负荷口径：按逐时累计（能量 kWh）为准。**
   结算、对账、监控构成都用小时积分能量。峰值（kW）仅作为派生的设备选型信号：
   每桶记录三个分项各自的峰值，另给 `peak_kw_upper_bound`（三分项峰值之和，上界），
   **不做“峰值折算”，峰值永不换算成负荷总量**。策略戳记 `basis=hourly_integral_kwh`。
2. **缺登记策略：补零（zero fill），不按上一条延续。**
   查询区间内没有任何记录的整点桶按全零合成，并标 `zero_filled=true`；有记录的桶带
   `records` 计数，可与“真实安静小时”区分。绝不从前一条静默延续数值。
3. **限流削峰：令牌桶。** 登记入口前置令牌桶（默认 200 批/小时、突发 250），
   超出返回 `429`，拒绝的批次不产生凭证、不入台账、不虚构负荷；令牌按事件时间确定性补充，
   可重启恢复、可测试。凭证只发给被接受的登记。
4. **积分性能预算：按跨越桶数封顶。** 每次折叠为 O(跨越小时桶数) 且单次写盘；
   超过 `max_span_buckets`（默认 256）直接 `400 performance budget`，防止野时间戳放大开销。
5. **同一口径：凭证能量即台账能量。** 登记在同一调用里算出唯一能量数字，凭证
   （`voucher.energy_kwh`）与台账入库分项复用该值；`GET /accounting/reconcile`
   以相对容差 1e-6 对账，健康检查 `accounting` 直接反映一致性。
6. **审计：** 每次登记写 `accounting-voucher`（接受/拒绝都留痕，含 batch_id、能量、小时），
   每次入台账写 `accounting-ledger`（分项、小时、能量；化霜按跨到的每个小时各一条）。
   凭证与核算结果因此同入一条审计链。
7. **幂等：** `batch_id` 唯一，重复登记返回 `409`；更正请用新批次，不覆盖历史。

所有选择集中在 `coldstore/accounting/report.py` 的 `POLICY`，版本戳 `accounting-v1`。
改动其中任一项即改变核算基准。

## 3. 监控路由

- `GET  /accounting` 策略、参数、凭证、区间、逐时桶构成、合计、峰值上界、限流余量
- `POST /accounting/register` 登记一批（经限流），返回凭证与落点小时桶
- `POST /accounting/voucher` 按 `batch_id` 查凭证
- `POST /accounting/defrost` 录入化霜区间并积分
- `POST /accounting/envelope` 录入环境温度采样
- `POST /accounting/load` 按时间段 `{start,end}` 查负荷构成（逐时、分项、零填充）
- `GET  /accounting/reconcile` 凭证 ↔ 台账一致性对账
- `POST /accounting/params` 设置物理系数 / 限流 / 积分预算
- `POST /accounting/limiter` 查询某时刻剩余令牌
- `POST /export/accounting` 逐时台账 CSV

## 4. 单位

质量 kg、温度 ℃、功率 kW、能量 kWh、时间 unix 秒。默认参数见
`report.py`：`cp=3.0 kJ/(kg·K)`、库温 −18 ℃、`U·A=0.05 kW/K`、环境 30 ℃、
化霜 6 kW、默认冷却时长 3600 s。
