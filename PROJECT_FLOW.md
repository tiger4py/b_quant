# b_quant 项目完整流程梳理

> 生成时间: 2026-06-24

---

## 一、整体架构（5 层）

```
[数据采集层] → [存储层] → [策略引擎层] → [应用层] → [输出层]
 BaoStock/AKShare   SQLite    16个策略+回测    Flask(8000)   QQ推送/Web面板
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

### 2.3 数据导入数据库

入口：`script/import_day_stock.py` — 将 CSV 导入到 SQLite 的 `stock_daily` 表。

### 2.4 数据库表结构（6张表）

| 表 | 说明 |
|---|---|
| `stock_info` | 股票基本信息（代码、名称、市场、上市日期、股本） |
| `stock_daily` | 日K线（OHLCV + 换手率 + PE_TTM），唯一索引 (code, trade_date) |
| `concept` | 同花顺概念板块 |
| `stock_concept` | 股票-概念多对多关联 |
| `concept_daily` | 概念指数日K线 |
| `backtest_cache` | 回测结果缓存 |

---

## 三、策略引擎层

### 3.1 核心策略：趋势跟随 (trend_following) ★ 当前主力

文件：`backtest/strategy/strategy_trend_following.py`

**策略理念（四维叠加）：**
1. **趋势过滤**：5日涨幅≥1% + 20日跌幅≥-8% + 40日最大回撤≤20%（排除底部反弹，只做上升趋势）
2. **量能放大**：5日均量/20日均量 ≥1.3 且 ≤5.0（排除异常爆量），近3日量能不萎缩
3. **量价配合**：上涨日量 / 下跌日量 ≥1.1（涨放量、跌缩量），至少连涨1天
4. **位置合理**：站上MA10+MA20、距20日高点≤15%、不超过MA20的15%

**买入条件（全部满足才触发）：**

```
条件1: 5日涨幅 ≥ 1%                    ← 短线有动能
条件2: 20日跌幅 ≥ -8%                  ← 中期趋势不差
条件3: 40日最大回撤 ≤ 20%              ← 不是下跌趋势中的反弹
条件4: 收盘站上 MA10 和 MA20           ← 均线多头
条件5: 距20日高点 ≤ 15%                ← 接近前高，上方压力小
条件6: 收盘 ≤ MA20 × 1.15             ← 不追高
条件7: 量比(vol_5d/vol_20d) ∈ [1.3, 5.0]  ← 放量但不异常爆量
条件8: 近3日均量 / 前3日均量 ≥ 1.0     ← 量能不萎缩
条件9: 涨跌量比 ≥ 1.1                  ← 涨放量、跌缩量
条件10: 连涨 ≥ 1天                     ← 动量确认
条件11: 次日非一字涨停（可买入）        ← 排除买不到的
条件12: 次日仍站上MA10/MA20            ← 入场日确认
```

**关键设计：次日收盘价成交** — 信号产生于第 i 天，实际以第 i+1 天收盘价买入，不含信号日收益，避免未来函数。

**卖出条件（任一触发即离场）：**

```
1. 硬止损 -8%（相对买入价）
2. 止盈 +25%
3. 跌破 MA20（趋势破坏）
4. 量能崩塌（量比 < 0.7）
5. 高位回撤 -10%（从持仓高点）
6. 持仓超15天到期
7. 量价背离（涨但量比降至 < 1.0）
```

### 3.2 辅助策略：波动率V反 (volatility_breakout)

文件：`backtest/strategy/strategy_volatility_breakout.py`

用于 `daily_guide.py` 每日指导报告的候选股筛选，作为趋势跟随的补充视角。

**策略理念（三维叠加）：**
1. **波动率异动**：长期平稳的股票（60日波动率<2.5%），短期波动突然放大
2. **V形反转**：快速下跌后3~8天内V型拉回 → 洗盘/底部确认
3. **大盘脱敏**：个股独立于大盘走强

**买入条件（全部满足才触发）：**

```
条件1: 60日波动率 < 2.5%          ← 历史平稳
条件2: vol_5d / vol_60d ≥ 1.3     ← 波动异动
条件3: V反形态检测成功             ← 跌≥3% + 涨≥1.5% + 恢复≥40%
条件3.5: V反恢复 ≤ 20%            ← 排除过度拉升
条件3.6: 近3天无涨停               ← 排除已拉起的
条件4: 当日跌幅 > -3%             ← 不接飞刀
条件5: 右侧放量 ≥ 左侧80%         ← 量能确认
```

**卖出条件：** 止损-10%、V反失效（跌破底部）、回撤-12%、止盈+20%、波动率消退、持仓超15天、单日暴跌>8%

### 3.3 其他14个策略

| 策略 | 文件 | 核心理念 |
|---|---|---|
| 吸筹试盘 | strategy_accumulation_probe.py | 低波动后放量上影线 |
| MACD金叉 | strategy_macd_cross.py | MACD金叉 + 量能确认 |
| 双均线趋势 | strategy_dual_ma_trend.py | 双均线 + 趋势过滤 |
| 布林回归 | strategy_bollinger_reversion.py | 布林带下轨回归 |
| ATR跟踪 | strategy_atr_trailing.py | ATR动态止损止盈 |
| KDJ金叉 | strategy_kdj_cross.py | KDJ指标交叉 |
| RSI反转 | strategy_rsi_reversal.py | RSI超买超卖 |
| 均线交叉 | strategy_ma_cross.py | 短期均线上穿长期均线 |
| 20日突破 | strategy_breakout_20.py | 突破20日高点 |
| 60日动量 | strategy_momentum_60.py | 60日动量效应 |
| 放量上涨 | strategy_price_volume_rising.py | 量价齐升 |
| 量能突破 | strategy_volume_breakout.py | 成交量突破 |
| 事件波动 | strategy_event_volatility.py | 事件驱动波动 |
| 主题轮动 | strategy_theme_phase.py | 概念板块轮动 |

### 3.4 策略统一接口

每个策略必须实现：
- `META` — 策略元信息（id, name, description）
- `generate_signals(bars)` — 生成买卖信号列表
- 可选 `market_gate(date, market_stats)` — 大盘过滤器

### 3.5 回测引擎

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

**多策略轮动** — `backtest/portfolio.py` `run_multi_strategy_backtest()`：
```
趋势跟随 + V反自由竞争总仓位
  极端恐慌(breadth<0.30) → 禁用趋势跟随，只用V反
  强势(breadth>0.45)     → 两者自由竞争
```

---

## 四、应用层

### 4.1 每日自动化管线（核心）★

#### 4.1.1 盘前推送 — daily_push.py（Claude 定时 8:30）

文件：`script/daily_push.py`

```
Step 1: update_daily.py        → 更新K线数据
Step 2: daily_trade.py         → 生成交易计划 + QQ推送
```

#### 4.1.2 收盘全流程 — daily_full_flow.py（Flask 调度 19:30）

文件：`script/daily_full_flow.py`

```
Step 1: update_daily.py                          → 更新K线数据
Step 2: run_strategy_market_backtest.py           → 趋势跟随全市场回测(1000天, max5仓)
       --strategy trend_following
Step 3: push_latest_trades.py                    → 推送当日买卖信号 + 持仓到QQ
```

#### 4.1.3 最新交易推送 — push_latest_trades.py

文件：`script/push_latest_trades.py`

```
从回测缓存读取趋势跟随策略的最新交易
  → 提取当日买入/卖出信号
  → 展示当前持仓（期末持仓 = 未平仓）
  → 推送QQ
```

### 4.2 每日趋势跟随扫描 (daily_scan_push.py)

文件：`script/daily_scan_push.py`

```
全市场无状态扫描 → 检测趋势跟随策略买入条件
  → 4维评分(趋势30 + 量能25 + 配合25 + 位置20)
  → 排序 → 打印 + QQ推送
```

这是趋势跟随策略的实时扫描版，不依赖回测缓存，直接从数据库读取最新K线进行买入条件检测。

### 4.3 每日指导报告 (daily_guide.py) — 辅助分析

文件：`script/daily_guide.py`

基于**波动率V反策略**的辅助分析工具，提供大盘评估 + V反候选股筛选。不作为主要交易依据，用于补充视角。

**分4个阶段：**

```
Phase 1 — 大盘环境评估
  数据源: _build_market_stats() 从全市场K线计算每日涨跌比、跌停数、成交额
  评分维度:
    广度(40%): 涨跌比百分位，越高越好
    风险(35%): 跌停数百分位，越少越好（≥50只封顶30分）
    量能(25%): 成交额/MA20百分位
  信号灯: ≥65 GREEN | ≥35 YELLOW | <35 RED

Phase 2 — 候选股扫描
  全市场扫描 → 检查每只股最新日是否满足波动率V反买入条件
  → 输出候选股列表（含V反形态、波动率、量能、价格位置等详细信息）

Phase 3 — 6维评分排序
  横盘蓄力(25%) > 波动信号(22%) > V反质量(20%) > 稳定性(13%) > 量能(10%) > 趋势(10%)
  重点推荐 ≥60分 | 可关注 ≥40分 | 一般 <40分

Phase 4 — 决策建议
  GREEN → 积极参与，正常仓位
  YELLOW → 谨慎参与，控制50%仓位
  RED → 建议观望，不操作
```

### 4.4 持仓预警 (check_holdings_alert.py)

文件：`script/check_holdings_alert.py`

```
从 portfolio.json 读取实盘持仓
  → 从新浪实时行情获取现价
  → 计算趋势跟随平仓线:
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
  app_id + client_secret + push_targets列表
```

### 5.2 Flask Web 面板

文件：`main.py` — 端口 8000

**页面路由：**

| 路由 | 页面 | 功能 |
|---|---|---|
| `/` | index.html | 首页 |
| `/stocks` | stocks.html | 股票浏览 |
| `/concepts` | concepts.html | 概念板块 |
| `/strategy-backtest` | strategy_backtest.html | 策略回测面板 |
| `/trading` | trading.html | 实盘交易面板（趋势跟随持仓+策略对比） |

**API 路由（20+个）：**

| 类别 | 端点 | 功能 |
|---|---|---|
| 股票 | `/api/stocks` | 股票列表（分页+搜索） |
| 股票 | `/api/stocks/<code>/daily` | 个股日K线 |
| 股票 | `/api/stocks/prices` | 批量获取收盘价 |
| 回测 | `/api/backtest/strategies` | 策略列表 |
| 回测 | `/api/backtest/market-overview` | 全市场策略表现排名 |
| 回测 | `/api/backtest/stock` | 单股回测 |
| 回测 | `/api/backtest/battle` | 策略对战(多策略同一批股票) |
| 回测 | `/api/backtest/stability` | 策略稳定性测试(多参数组合) |
| 回测 | `/api/backtest/market/<id>` | 某策略全市场回测缓存 |
| 交易 | `/api/trading/state` | 实盘状态(持仓+趋势跟随策略信号对比) |
| 交易 | `/api/trading/buy` | 记录买入 |
| 交易 | `/api/trading/sell` | 记录卖出 |
| 概念 | `/api/concepts` | 概念列表 |
| 概念 | `/api/concepts/<code>/daily` | 概念日K线 |
| 下载 | `/api/download/stocks` | 触发下载股票列表 |
| 下载 | `/api/download/daily` | 触发下载日K线 |

---

## 六、定时任务体系

### 6.1 Flask 内置调度器

`main.py` — `_scheduler_loop` 线程

```
每个工作日 19:30 触发:
  1. _run_daily_update_job() → 更新股票+概念数据
  2. _run_push_job() → daily_full_flow.py (趋势跟随回测+推送)
```

### 6.2 Claude Code 定时任务

`.claude/scheduled_tasks.json`：

```
每个工作日 8:30 → 运行 daily_push.py (更新数据 + 趋势跟随交易计划推送)
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
                    ▼                ▼                ▼
            update_daily.py   download_concepts  import_day_stock
            (CSV按日期存储)   (CSV按日期存储)    (导入SQLite)
                    │                │                │
                    └────────────────┼────────────────┘
                                     ▼
                          ┌──────────────────┐
                          │   SQLite 数据库   │
                          │  stock_info       │
                          │  stock_daily      │
                          │  concept          │
                          │  stock_concept    │
                          │  concept_daily    │
                          │  backtest_cache   │
                          └──────┬───────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         ▼                       ▼                       ▼
   ┌──────────────┐    ┌──────────────────┐    ┌──────────────────┐
   │ 趋势跟随管线  │    │  辅助分析         │    │  回测系统         │
   │ (核心★)      │    │                  │    │                  │
   ├──────────────┤    │ daily_guide.py   │    │ run_strategy_    │
   │ daily_scan_  │    │ (波动率V反候选    │    │ market_backtest  │
   │ push.py      │    │  + 大盘评估)      │    │ (全市场回测       │
   │ (实时扫描)    │    │                  │    │  + 缓存结果)      │
   ├──────────────┤    └────────┬─────────┘    └────────┬─────────┘
   │ daily_full_  │             │                       │
   │ flow.py      │             │                       │
   │ (收盘全流程)  │             │                       │
   ├──────────────┤             │                       │
   │ push_latest_ │             │                       │
   │ trades.py    │             │                       │
   │ (交易推送)    │             │                       │
   └──────┬───────┘             │                       │
          │                     │                       │
          └─────────────────────┼───────────────────────┘
                                ▼
                    ┌──────────────────────┐
                    │   输出 & 决策         │
                    │  ├─ 控制台报告        │
                    │  ├─ JSON文件          │
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
           使用趋势跟随卖出参数:
           止损-8% / 回撤-10% / 量能崩塌0.7
```

---

## 八、日常操作节奏

| 时间 | 动作 | 策略 | 脚本 |
|---|---|---|---|
| **8:30** (工作日) | 盘前推送：更新数据 + 交易计划 | 趋势跟随 | `daily_push.py` (Claude定时) |
| **盘中随时** | 实时持仓预警（趋势跟随平仓线） | 趋势跟随 | `check_holdings_alert.py --loop` |
| **盘中按需** | 趋势跟随实时扫描 | 趋势跟随 | `daily_scan_push.py` |
| **15:00后** | 收盘更新：拉取当日K线 | — | `update_daily.py` |
| **19:30** (工作日) | 收盘全流程：更新 → 趋势跟随回测 → 推送 | 趋势跟随 | `daily_full_flow.py` (Flask调度) |
| **按需** | 每日指导报告（V反辅助分析） | 波动率V反 | `daily_guide.py --push` |

---

## 九、关键设计特点

1. **趋势跟随为主，V反为辅**：日常自动化管线（盘前推送+收盘全流程+持仓预警+Web面板）全部基于趋势跟随策略；波动率V反通过 daily_guide.py 提供辅助视角
2. **策略与回测分离**：策略只输出信号，回测引擎独立处理资金管理
3. **无未来函数**：趋势跟随采用次日收盘价成交，信号日和入场日分离
4. **次日一字涨停过滤**：买入前检查次日是否一字涨停，排除买不到的情况
5. **多策略竞争**：趋势跟随+V反按市场广度动态分配，极端恐慌时仅用V反
6. **CSV+DB双存储**：原始数据存CSV（可追溯），导入SQLite后用于查询分析
7. **三层风控**：个股止损-8%(-10%) + 组合熔断-25% + 大盘MA60仓位管理
8. **实时+回测双轨**：daily_scan_push.py 实时扫描 + daily_full_flow.py 回测推送，互相印证
