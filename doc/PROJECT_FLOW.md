# b_quant 项目完整流程梳理

> 更新: 2026-07-02

---

## 一、整体架构（5 层）

```
[数据采集层] → [存储层] → [策略引擎层] → [应用层] → [输出层]
 BaoStock/AKShare   SQLite    3个策略+回测    Flask(8000)   QQ推送/Web面板
```

---

## 二、数据采集层

### 2.1 股票日K线 — BaoStock

入口：`script/update_daily.py` — `update_stocks()`

```
流程:
  1. 交易日校验 (AKShare获取A股交易日历，非交易日自动跳过)
  2. 从 stock_info 表读取所有 status=1, type='1' 的正常上市股票
  3. 逐只通过 BaoStock API 拉取 OHLCV+换手率+PE 数据
  4. 断线自动重连（连续5次失败/网络错误触发重登）
  5. 按日期分文件存为 CSV: data/day_stock/YYYYMM/YYYY-MM-DD.csv
```

### 2.2 概念指数 — AKShare

入口：`script/update_daily.py` — `update_concepts()`

```
流程:
  1. 从 concept 表读取所有同花顺概念板块
  2. 逐概念通过 ak.stock_board_concept_index_ths() 拉取指数日线
  3. 按日期分文件存为 CSV: data/day_concept/YYYYMM/YYYY-MM-DD.csv
```

### 2.3 CSV 导入数据库

入口：`script/import_day_stock.py` — 将 CSV 导入到 SQLite 的 `stock_daily` / `concept_daily` 表。INSERT OR REPLACE 策略，可重复执行。

### 2.4 数据库表结构（6张表）

| 表 | 说明 |
|---|---|
| `stock_info` | 股票基本信息（代码、名称、市场、上市日期、股本） |
| `stock_daily` | 日K线（OHLCV + 换手率 + PE_TTM），唯一索引 (code, trade_date) |
| `concept` | 同花顺概念板块 |
| `stock_concept` | 股票-概念多对多关联 |
| `concept_daily` | 概念指数日K线 |
| `backtest_cache` | 回测结果缓存（已弃用，改为文件归档） |

---

## 三、策略引擎层



**策略理念：** 只在市场恐慌时出手的逆势策略。恐慌→深跌→卖压衰竭三重确认。

**买入条件（全部满足才触发）：**

```
条件1: 市场广度 < 25%              ← 极度恐慌
条件2: 收盘 < MA60 × 0.92          ← 深度超跌
条件3: 5日跌幅 ∈ [-8%, -1%]        ← 卖压衰竭（不是崩盘中）
```

**卖出条件：**
- ATR 动态止损（2.5x ATR，min 8% / max 22%）
- 移动止盈（盈利 >10% 后，回落 -15% 触发）
- 90 天到期
- 大盘回暖（市场广度 > 60%）→ 减半仓

### 3.2 策略2: 维加斯隧道 (vegas_tunnel)

文件：`backtest/strategy/strategy_vegas_tunnel.py`

**策略理念：** 基于 EMA 均线系统的趋势跟随策略。

**买入条件：** EMA12 上穿 EMA144/169 隧道 + 隧道多头排列 + EMA576 长期趋势过滤 + 量能确认

**卖出条件：** EMA12 下穿隧道 / 高位回撤 -12% / 40 天到期

### 3.3 策略3: Alpha042 量价背离 (alpha042)

文件：`backtest/strategy/strategy_alpha042.py`

**策略理念：** 基于国泰君安 191 因子库因子 #042 — 缩量新高是吸筹信号。

**买入条件：** 10日 correlation(high, volume) < -0.25 + 波动率放大 1.2x-5.0x + 接近20日高点

**卖出条件：** correlation > 0.50（量价同步=主力出货）/ 30 天到期

### 3.4 补充分析: 波动率V反（内联在 daily_guide.py）


**买入条件：** 60日波动率<2.5% + 波动异动 + V反形态（跌≥3%后涨≥1.5%，恢复≥40%）+ 无涨停 + 右侧放量

**卖出条件：** 止损-10% / V反失效 / 回撤-12% / 止盈+20% / 持仓超15天

### 3.5 策略统一接口

每个策略必须实现：
- `META` — 策略元信息（id, name, description）
- `generate_signals(bars)` — 生成买卖信号列表
- 可选 `market_gate(date, market_stats)` — 大盘过滤器

### 3.6 回测引擎

**单股回测** — `backtest/engine.py`：
```
初始资金 → 逐日遍历 → 信号触发买入(全仓) → 信号触发卖出 → 计算收益/回撤/胜率
```

**全市场组合回测** — `backtest/portfolio.py`：
```
初始资金 → 最多N只持仓 → 按成交额排序择优买入 → 动态仓位管理
  ├─ 大盘MA60仓位管理：大盘<MA60 → 仓位减半
  └─ 组合熔断：回撤>-25% → 强制降仓到1只
```

**多策略轮动** — 支持两个策略竞争总仓位，按市场广度动态分配。

---

## 四、应用层

### 4.1 收盘全流程 — daily_full_flow.py（Flask 调度 19:30）

文件：`script/daily_full_flow.py`

```
Step 1:    update_daily.py              → 更新K线数据（BaoStock + AKShare → CSV）
Step 1.5:  import_day_stock.py          → CSV 导入 SQLite 数据库
Step 3:    push_latest_trades.py        → 推送当日买卖信号 + 持仓到QQ
```

### 4.2 每日回顾报告 — daily_review.py

文件：`script/daily_review.py`

```
汇总所有策略信号 + 实盘持仓 + 大盘评估 → 生成 data/reviews/YYYY-MM-DD.md
  ├─ 大盘环境（信号灯 + 涨跌比 + 跌停数 + 成交额）
  ├─ V反候选（daily_guide 评分排序）
  ├─ 价量齐升候选（scan_price_volume_rising）
  ├─ 实盘持仓状态
  └─ 今日操作记录
```

### 4.3 每日指导报告 — daily_guide.py

文件：`script/daily_guide.py`

```
Phase 1 — 大盘环境评估（广度40% + 风险35% + 量能25%）
Phase 2 — V反候选股全市场扫描
Phase 3 — 6维评分排序（横盘蓄力 > 波动信号 > V反质量 > 稳定性 > 量能 > 趋势）
Phase 4 — 决策建议（GREEN积极参与 / YELLOW控制仓位 / RED建议观望）
```

### 4.4 价增量增扫描 — scan_price_volume_rising.py

文件：`script/scan_price_volume_rising.py`

```
全市场扫描价量齐升股票 → 4维评分(价格30 + 量能30 + 配合25 + 质量15) → 排序 → 打印 + QQ推送
```

### 4.5 持仓预警 — check_holdings_alert.py

文件：`script/check_holdings_alert.py`

```
从 portfolio.json 读取实盘持仓
  → 从新浪实时行情获取现价
  → 检测平仓条件:
     止损-8%、高位回撤-10%、破MA10/MA20、量能崩塌(量比<0.7)、量价背离
  → 触发预警 → QQ推送
  → 支持 --loop 循环监控模式(每60秒)
```

---

## 五、输出层

### 5.1 QQ推送 (QQPusher)

文件：`models/qq_webhook.py`

```
QQPusher 类:
  ├─ Token管理(自动刷新)
  ├─ 三种推送: send_user_message / send_group_message / send_channel_message
  ├─ push_to_all: 向所有配置目标推送
  └─ push_long_text: 长文本自动分段(>2000字符拆分)

配置: data/qq_config.json
```

### 5.2 Flask Web 面板

文件：`main.py` — 端口 8000

**页面路由：**

| 路由 | 页面 | 功能 |
|---|---|---|
| `/` | index.html | 首页 |
| `/stocks` | stocks.html | 股票浏览（ECharts K线图 + 多股对比） |
| `/concepts` | concepts.html | 概念板块（K线图 + 成分股） |
| `/strategy-backtest` | strategy_backtest.html | 策略回测面板 |
| `/trading` | trading.html | 实盘交易面板（持仓+策略信号对比） |

**API 路由（20+个）：**

| 类别 | 端点 | 功能 |
|---|---|---|
| 股票 | `/api/stocks` | 股票列表（分页+搜索） |
| 股票 | `/api/stocks/<code>/daily` | 个股日K线 |
| 回测 | `/api/backtest/strategies` | 策略列表 |
| 回测 | `/api/backtest/market-overview` | 全市场策略表现 |
| 回测 | `/api/backtest/stock` | 单股回测 |
| 回测 | `/api/backtest/battle` | 策略对战 |
| 回测 | `/api/backtest/stability` | 策略稳定性测试 |
| 回测 | `/api/backtest/market/<id>` | 某策略全市场回测缓存 |
| 交易 | `/api/trading/state` | 实盘状态 |
| 交易 | `/api/trading/buy` | 记录买入 |
| 交易 | `/api/trading/sell` | 记录卖出 |
| 概念 | `/api/concepts` | 概念列表 |
| 概念 | `/api/concepts/<code>/daily` | 概念日K线 |

---

## 六、定时任务体系

### 6.1 Flask 内置调度器

`main.py` — `_scheduler_loop` 线程

```
每个工作日 19:30 触发:
  1. _run_daily_update_job() → 更新股票+概念数据 + CSV导入数据库
```

---

## 七、完整数据流图

```
                          ┌──────────────────────────────────┐
                          │         外部数据源                │
                          │  BaoStock (K线) + AKShare (概念) │
                          └──────────┬───────────────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    ▼                                 ▼
            update_daily.py                import_day_stock.py
            (CSV按日期存储)                 (CSV → SQLite)
                    │                                 │
                    └────────────────┼────────────────┘
                                     ▼
                          ┌──────────────────┐
                          │   SQLite 数据库   │
                          │  (658MB, ~4900股) │
                          └──────┬───────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         ▼                       ▼                       ▼
   ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐
   │ 日终管线      │    │  辅助分析         │    │  回测系统         │
   ├──────────────┤    ├──────────────────┤    ├──────────────────┤
   │ daily_full_  │    │ daily_guide.py   │    │ run_strategy_    │
   │ flow.py      │    │ (V反候选+大盘)    │    │ market_backtest  │
   │ (market_     │    │                  │    │ (全市场回测)      │
   │ bottom回测   │    │ daily_review.py  │    │                  │
   │ + QQ推送)    │    │ (每日回顾汇总)    │    │ analyze_trades   │
   │              │    │                  │    │ (深度分析)        │
   │ push_latest_ │    │ scan_price_      │    │                  │
   │ trades.py    │    │ volume_rising.py │    │ optimize_strategy│
   │ (交易推送)    │    │ (价量扫描)        │    │ (参数优化)        │
   └──────┬───────┘    └────────┬─────────┘    └────────┬─────────┘
          │                     │                       │
          └─────────────────────┼───────────────────────┘
                                ▼
                    ┌──────────────────────┐
                    │   输出 & 决策         │
                    │  ├─ 控制台报告        │
                    │  ├─ JSON文件          │
                    │  ├─ Markdown回顾报告  │
                    │  ├─ QQ推送            │
                    │  └─ Flask Web面板     │
                    └──────┬───────────────┘
                           │
                    ┌──────┴──────┐
                    ▼             ▼
              portfolio.json  trade_log.json
              (实盘持仓)       (交易记录)
                    │
                    ▼
           check_holdings_alert.py
           (实时平仓预警 → QQ推送)
```

---

## 八、日常操作节奏

| 时间 | 动作 | 脚本 |
|---|---|---|
| **盘中随时** | 实时持仓预警 | `check_holdings_alert.py --loop` |
| **盘中按需** | 价增量增扫描 | `scan_price_volume_rising.py` |
| **盘中按需** | V反候选扫描 | `daily_guide.py` |
| **15:00后** | 收盘更新：拉取当日K线 | `update_daily.py` |
| **19:30** (工作日) | 收盘全流程：更新→导入→回测→推送 | `daily_full_flow.py` (Flask调度) |
| **收盘后** | 每日回顾报告 | `daily_review.py --push` |
| **按需** | 交易深度分析 | `analyze_trades.py` |
| **按需** | 策略参数优化 | `optimize_strategy.py` |

---

## 九、关键设计特点

2. **策略与回测分离**：策略只输出信号，回测引擎独立处理资金管理
3. **无未来函数**：信号日和入场日分离，采用次日收盘价成交
4. **CSV+DB双存储**：原始数据存CSV（可追溯），导入SQLite后用于查询分析
5. **三层风控**：个股动态止损 + 组合熔断-25% + 大盘MA60仓位管理
6. **文件归档回测结果**：不再使用数据库缓存，回测结果存为 JSON 文件归档
