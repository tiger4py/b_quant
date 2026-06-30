# b_quant 项目完整流程梳理

> 最后更新: 2026-06-24

---

## 一、整体架构（5 层）

```
[数据采集层] → [存储层] → [策略引擎层] → [应用层] → [输出层]
 BaoStock/AKShare   SQLite    16个策略+回测    Flask(8000)   QQ推送/Web面板
```

---

## 二、数据采集层

### 2.1 股票日K线 — BaoStock

入口：[script/update_daily.py](script/update_daily.py) — `update_stocks()`

```
1. 交易日校验 (AKShare获取A股交易日历，非交易日自动跳过)
2. 从 stock_info 表读取所有 status=1, type='1' 的正常上市股票 (~4900只)
3. 逐只通过 BaoStock API 拉取 OHLCV+换手率+PE 数据
4. 断线自动重连（连续5次失败/网络错误触发重登）
5. 按日期分文件存为 CSV: data/day_stock/YYYYMM/YYYY-MM-DD.csv
```

### 2.2 概念指数 — AKShare

入口：[script/update_daily.py](script/update_daily.py) — `update_concepts()`

```
1. 从 concept 表读取所有同花顺概念板块
2. 逐概念通过 ak.stock_board_concept_index_ths() 拉取指数日线
3. 按日期分文件存为 CSV: data/day_concept/YYYYMM/YYYY-MM-DD.csv
```

### 2.3 数据导入

入口：[script/import_day_stock.py](script/import_day_stock.py) — 将 CSV 导入到 SQLite。

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

### 三大策略总览（全市场回测 2022~2026）

| 策略 | 收益 | 回撤 | 胜率 | 交易数 | 盈利因子 | 定位 |
|---|---|---|---|---|---|---|
| **market_bottom** | **+96.97%** | **29.23%** | 41.38% | 58 | **2.32** | 恐慌抄底，低频高质量 |
| trend_following | +11.10% | 52.90% | 43.57% | 723 | 1.03 | 每日追涨，高频但有信号瓶颈 |
| volatility_breakout | -6.84% | 61.15% | 40.95% | 1050 | 0.98 | 辅助分析，不独立交易 |

### 3.1 核心策略：大底抄底 (market_bottom) ★ 最佳

文件：[backtest/strategy/strategy_market_bottom.py](backtest/strategy/strategy_market_bottom.py)

**核心理念**：只在全市场恐慌（广度<25%）时买入深度超跌股，持有 60-90 天等修复反弹。

```
买入条件: 市场广度<25% + 股价跌破MA60超15% + 卖压衰竭(5日-5%~0%)
卖出条件: 止损-12% / 高位回撤-15% / 持仓90天到期
```

**关键特点**：4年仅58笔交易，低频高确定性，最大回撤仅29%，与趋势跟随互补。

### 3.2 辅助策略：趋势跟随 (trend_following) ★ 当前主力

文件：[backtest/strategy/strategy_trend_following.py](backtest/strategy/strategy_trend_following.py)

#### 策略理念（四维叠加）

1. **趋势过滤**：5日涨幅≥1% + 20日跌幅≥-8% + 40日最大回撤≤20%（排除底部反弹，只做上升趋势）
2. **量能放大**：5日均量/20日均量 ∈ [1.3, 5.0]，近3日量能不萎缩
3. **量价配合**：上涨日量/下跌日量 ≥1.1（涨放量、跌缩量），连涨≥1天
4. **位置合理**：站上MA10+MA20、距20日高点≤15%、不超过MA20的15%

#### 买入条件（12条全部满足）

```
条件1:  5日涨幅 ≥ 1%                    ← 短线有动能
条件2:  20日跌幅 ≥ -8%                  ← 中期趋势不差
条件3:  40日最大回撤 ≤ 20%              ← 非下跌趋势反弹（滚动峰值法计算）
条件4:  收盘站上 MA10 和 MA20           ← 均线多头
条件5:  距20日高点 ≤ 15%                ← 接近前高
条件6:  收盘 ≤ MA20 × 1.15             ← 不追高
条件7:  量比 ∈ [1.3, 5.0]               ← 放量但不异常爆量
条件8:  近3日均量/前3日均量 ≥ 1.0       ← 量能不萎缩
条件9:  涨跌量比 ≥ 1.1                  ← 涨放量、跌缩量
条件10: 连涨 ≥ 1天                      ← 动量确认
条件11: 次日非一字涨停                   ← 排除买不到的
条件12: 次日仍站上MA10/MA20             ← 入场日确认
```

**关键设计**：次日收盘价成交（信号日 i，成交日 i+1），无未来函数。

#### 卖出条件（8条，按优先级）

```
1. 硬止损 -8%
2. 止盈 +25%
3. 单日暴跌 >8%（紧急离场）
4. 跌破 MA20（趋势破坏）
5. 量能崩塌（量比 < 0.7）
6. 高位回撤 -10%（从持仓高点）
7. 持仓超15天到期
8. 量价背离（涨但量比降至 < 1.0）
```

#### 回测表现（2022-05-06 → 2026-06-23，4906只股票，max5仓）

| 指标 | 数值 |
|---|---|
| 总收益 | **+11.10%** |
| 最大回撤 | 52.90% |
| 胜率 | 43.57% |
| 交易数 | 723 |
| 盈利因子 | 1.03 |

#### 深度分析结论（718笔交易）

**最大出血点：**

| 卖出原因 | 笔数 | 胜率 | 合计盈亏 |
|---|---|---|---|
| 跌破MA20 | 243 | 1% | -176万 |
| 止损(-8%) | 94 | 0% | -158万 |
| 量价背离 | 230 | 100% | +142万 |
| 止盈(+25%) | 39 | 100% | +188万 |

**止盈 vs 止损对比**：两组在买入点的 5日涨幅（~10%）和 20日涨幅（~14%）几乎相同，策略事前无法区分"继续涨"和"马上反转"。多维度分析找到的最大区分维度是 20日价格位置（区分度 0.85），但直接硬过滤会误杀趋势中继的正常高位运行。

**结论**：策略框架的信号质量上限约 +11%。改进方向不是调参数，而是引入新的信息维度（如市场环境、板块强度、波动率结构等）。

### 3.3 辅助策略：波动率V反 (volatility_breakout)

文件：[backtest/strategy/strategy_volatility_breakout.py](backtest/strategy/strategy_volatility_breakout.py)

用于 [daily_guide.py](script/daily_guide.py) 的候选股筛选。全市场回测收益 -6.84%，交易1050笔，假信号多（75%因波动率消退离场），不适合独立交易。

### 3.4 策略统一接口

```python
META = {"id": "...", "name": "...", "description": "..."}

def generate_signals(bars) -> list[dict]:
    """返回 [{"date": "", "action": "buy/sell", "reason": ""}, ...]"""

def market_gate(date, market_stats) -> dict:
    """可选，返回 {"allowed": bool, "reasons": [str]}"""
```

### 3.6 回测引擎

**单股回测** — [backtest/engine.py](backtest/engine.py)：全仓买卖，逐日遍历。

**全市场组合回测** — [backtest/portfolio.py](backtest/portfolio.py)：
```
初始资金100万，最多5只持仓
买入排序: 按信号日前20日均成交额降序（★ 已修复，原用最新日期成交额有未来函数）
动态仓位: 大盘MA60仓位管理 + 组合熔断(-25%降仓到1只)
```

**重要修复**：2026-06-24 将买入排序从 `latest_amount`（最新日期成交额）改为 `amount_ma20`（信号日前20日均成交额），消除了未来函数。修复前收益虚高（190%~468%），修复后真实收益 ~11%。

---

## 四、应用层

### 4.1 每日自动化管线（核心）★

**盘前 8:30** — `daily_push.py`（Claude Code 定时）：
```
update_daily.py → daily_trade.py → QQ推送
```

**收盘 19:30** — `daily_full_flow.py`（Flask 调度）：
```
update_daily.py → run_strategy_market_backtest.py --strategy trend_following → push_latest_trades.py
```

### 4.2 回测命令行

```bash
# 默认全量（2022-05-06 → 数据库最新）
python script/run_strategy_market_backtest.py --strategy trend_following --max-positions 5

# 指定日期范围（★ 新增 --start/--end，--days 已移除）
python script/run_strategy_market_backtest.py --strategy trend_following --max-positions 5 \
    --start 2024-01-01 --end 2026-06-22
```

### 4.3 实盘工具

| 脚本 | 功能 |
|---|---|
| [daily_scan_push.py](script/daily_scan_push.py) | 趋势跟随全市场实时扫描 + QQ推送 |
| [daily_guide.py](script/daily_guide.py) | 波动率V反每日指导报告（大盘评估+候选股） |
| [check_holdings_alert.py](script/check_holdings_alert.py) | 持仓实时预警（止损/回撤/均线/量能） |

---

## 五、输出层

### 5.1 QQ推送

文件：[models/qq_webhook.py](models/qq_webhook.py)

QQPusher 类：Token管理 → send_user/group/channel → push_to_all → push_long_text(自动分段)

配置：[data/qq_config.json](data/qq_config.json)

### 5.2 Flask Web 面板

文件：[main.py](main.py) — 端口 8000

5 个页面：首页、股票浏览、概念板块、策略回测、实盘交易。20+ 个 API 端点。

---

## 六、定时任务

| 时间 | 触发 | 动作 |
|---|---|---|
| 8:30 工作日 | Claude Code | `daily_push.py`（更新+交易计划推送） |
| 19:30 工作日 | Flask 调度器 | `daily_full_flow.py`（更新+回测+推送） |

---

## 七、完整数据流

```
BaoStock/AKShare → update_daily.py (CSV) → import_day_stock.py → SQLite
                                                                     │
              ┌──────────────────────────────────────────────────────┤
              ▼                                                      ▼
   ┌─────────────────────┐                           ┌──────────────────────┐
   │ 趋势跟随管线 (核心)   │                           │ 辅助分析               │
   │ daily_full_flow.py   │                           │ daily_guide.py (V反)   │
   │ daily_scan_push.py   │                           │ daily_scan_push.py     │
   │ push_latest_trades   │                           └──────────┬───────────┘
   └──────────┬───────────┘                                      │
              │                                                  │
              └──────────────────────┬───────────────────────────┘
                                     ▼
                          QQ推送 / JSON报告 / Flask面板
                                     │
                              portfolio.json (实盘)
                              trade_log.json (交易记录)
                                     │
                          check_holdings_alert.py (盘中预警)
```

---

## 八、关键发现与教训

1. **market_bottom 是最好的策略**：只在大盘恐慌时出手，4年58笔+97%，回撤仅29%，是当前唯一真正有效的策略。
2. **未来函数陷阱**：回测引擎用 `latest_amount` 排序买入候选，导致跨日期结果不可比。修复后趋势跟随从 +190% 跌至 +11%。
3. **趋势跟随信号质量上限**：止盈和止损在买入点特征高度重叠，用简单维度无法事前区分。策略上限约 +11%。
4. **波动率V反不适合独立交易**：1050笔交易中 75% 因波动率消退离场，噪音太大。
5. **低频 ≠ 低效**：market_bottom 4年仅58笔交易，但每笔质量高，最终收益远超高频的趋势跟随。
