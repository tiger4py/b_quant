# b_quant - A股量化分析系统

## 项目概述
A股量化选股 + 回测 + QQ推送系统。3个回测策略 + 1个V反扫描 + Web面板。

## 核心文件

| 文件 | 作用 |
|------|------|
| `script/daily_review.py` | **每日回顾报告**：汇总所有策略信号 + 持仓 + 大盘评估 |
| `script/daily_guide.py` | 每日指导报告：大盘评估 + V反候选股扫描 + 评分排序 |
| `script/daily_full_flow.py` | 收盘全流程：更新数据 → 导入DB → market_bottom回测 → QQ推送 |
| `script/update_daily.py` | 从BaoStock/AKShare更新日K线和概念指数数据（CSV） |
| `script/import_day_stock.py` | 将CSV导入SQLite数据库 |
| `script/push_latest_trades.py` | 推送最新回测交易信号到QQ |
| `script/check_holdings_alert.py` | 持仓实时预警（止损/回撤/量能崩塌检测） |
| `script/scan_price_volume_rising.py` | 价增量增策略每日候选扫描 |
| `script/run_strategy_market_backtest.py` | 全市场回测入口 |
| `script/analyze_trades.py` | 回测交易深度分析（生成Excel报告） |
| `script/optimize_strategy.py` | 策略参数自动优化 |
| `backtest/strategy/strategy_market_bottom.py` | **大底抄底策略**（市场恐慌+深跌+ATR动态止损） |
| `backtest/strategy/strategy_vegas_tunnel.py` | **维加斯隧道策略**（EMA12/144/169/576均线系统） |
| `backtest/strategy/strategy_alpha042.py` | **Alpha042量价背离**（correlation(high,volume)因子） |
| `models/qq_webhook.py` | QQ机器人推送（`QQPusher`类）+ Webhook回调 |
| `main.py` | Flask Web面板 (端口8000) |

## 常用命令

```bash
# 每日收盘后：更新数据 → 导入DB → 回测 → 推送
python script/daily_full_flow.py

# 生成每日回顾报告
python script/daily_review.py              # 今日报告
python script/daily_review.py --push       # 生成并QQ推送
python script/daily_review.py --last 5     # 最近5天

# 每日指导报告（V反候选+大盘评估）
python script/daily_guide.py
python script/daily_guide.py --push
python script/daily_guide.py --short       # 简短版

# 全市场回测（3个策略可选：market_bottom / vegas_tunnel / alpha042）
python script/run_strategy_market_backtest.py --strategy market_bottom --days 1000 --max-positions 5

# 价增量增扫描
python script/scan_price_volume_rising.py --push

# 持仓预警监控
python script/check_holdings_alert.py --loop

# 交易分析
python script/analyze_trades.py --strategy market_bottom

# 启动QQ Webhook服务
python models/qq_webhook.py
```

## 三大策略

### 1. market_bottom（大底抄底）— 日终管线主力
- 买入：市场恐慌（广度<25%）+ 深跌（低于MA60>8%）+ 卖压衰竭
- 卖出：ATR动态止损（2.5x ATR）/ 移动止盈 / 90天到期
- 只在恐慌时出手的逆势策略

### 2. vegas_tunnel（维加斯隧道）
- 买入：EMA12上穿EMA144/169隧道 + EMA576过滤 + 量能确认
- 卖出：EMA12下穿隧道 / 高位回撤-12% / 40天到期

### 3. alpha042（量价背离）
- 买入：10日correlation(high,volume) < -0.25 + 波动率放大 + 接近20日高点
- 卖出：correlation > 0.50 / 30天到期

## 波动率V反（内联在 daily_guide.py）

### 买入条件（全部满足）
1. 60日波动率 < 2.5%（历史平稳）
2. vol_5d/vol_60d ≥ 1.3（波动异动）
3. V反形态：跌≥3%后涨≥1.5%，恢复≥40%
4. 近3天无涨停（防止追高）
5. V反恢复 ≤ 20%（排除已经拉起来的）
6. 当日跌幅 > -3%（不接飞刀）
7. 右侧放量 ≥ 左侧80%

### 卖出条件
止损-10%、V反失效（跌破底部）、回撤-12%、止盈+20%、波动率消退、持仓超15天

### 候选股评分维度
横盘蓄力(25%) > 波动信号(22%) > V反质量(20%) > 稳定性(13%) > 量能(10%) > 趋势(10%)

### 大盘评估
广度(40%) + 跌停风险(35%) + 量能(25%) → 60日百分位评分 → GREEN/YELLOW/RED

## 配置
- 数据库: `data/stock.db` (SQLite, ~4900只A股)
- QQ推送: `data/qq_config.json`
- 策略参数: 各 `backtest/strategy/strategy_*.py` 顶部常量
- 评分参数: `script/daily_guide.py` 顶部常量

## 编码规范

### 命名
- **常量**: `UPPER_SNAKE_CASE`（如 `VOL_STABLE_MAX`, `GREEN_THRESHOLD`）
- **函数/变量**: `snake_case`（如 `generate_signals`, `daily_vol`）
- **私有函数**: 前缀 `_`（如 `_detect_v_reversal`, `_check_buy_conditions`）
- **类**: `PascalCase`（如 `QQPusher`, `StockDaily`）
- **策略入口函数**: `generate_signals(bars)` 和 `market_gate(date, market_stats)` 是公开API，不加下划线

### 注释和文档
- **注释用中文**，代码标识符用英文
- 模块顶部用多行中文 docstring 说明用途，复杂模块要写用法示例
- 函数用 Google 风格 docstring（`参数:`, `返回:`），中文写
- 算法逻辑用行内注释解释「为什么」，不只是「是什么」
- 分节用 `# ======== 标题 ========` 分隔

### 导入顺序
1. 标准库 (`import sys, json, os, re`)
2. `ROOT_DIR = Path(__file__).resolve().parents[N]` + `sys.path.insert`
3. 第三方库 (`from sqlalchemy import ...`)
4. 项目内部 (`from config import ...`, `from models.stock import ...`, `from backtest...`)

### 数据流
- 复杂返回值用 `dict`，不用自定义类或 namedtuple
- 失败返回 `None` 或 `False`，不抛异常
- 外部调用（HTTP、数据库）用 `try/except` 兜底，打印错误并继续

### 函数设计
- 工具函数短小（3-20行），纯计算，无副作用
- 编排函数可以长（100+行），但逻辑要按阶段分段注释
- 策略参数放在模块顶部常量区，方便调参

### 输出
- 用 `print()` 做日志，不用 logging 模块
- Windows 终端编码用 `sys.stdout.reconfigure(encoding='utf-8')` 兜底
- 不用 emoji，用 `[GREEN]`/`[RED]` 这类纯文本标签
