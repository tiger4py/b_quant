# AGENTS.md

本文件给后续代码代理使用，目标是在不了解项目上下文时，能快速、安全地接手 `b_quant`。

## 项目概览

这是一个 Python 量化研究、回测、每日复盘和实盘记录工具。核心链路是：

1. 通过 BaoStock / AKShare 拉取 A 股、概念、ETF 数据。
2. 原始日线先落 CSV，再导入 SQLite。
3. 策略模块生成买卖信号。
4. 回测引擎负责资金、仓位、组合层风控和结果统计。
5. Flask Web 面板展示股票、概念、策略回测、实盘交易和策略实验室。
6. 每日脚本生成复盘、候选股、持仓预警，并可通过 QQ webhook 推送。

## 目录职责

- `main.py`: Flask 应用入口，默认监听 `0.0.0.0:8000`；包含页面路由、API、交易记录接口、策略实验室接口和内置定时任务。
- `config.py`: 全局配置，包含 `DATA_DIR`、`DATABASE_URL`、K 线字段、下载参数和同花顺 cookie。
- `models/`: SQLAlchemy ORM 模型和 QQ webhook 推送逻辑。
- `logic/`: 数据下载、因子预计算、回测缓存/归档加载、进度状态等通用逻辑。
- `backtest/`: 回测核心。
  - `registry.py`: 自动发现 `backtest/strategy/strategy_*.py`。
  - `engine.py`: 单标的回测。
  - `portfolio.py`: 全市场/组合回测、市场状态、仓位和熔断逻辑。
  - `strategy/`: 策略实现。
- `script/`: 命令行任务。
  - `update_base_data/update_daily.py`: 拉取股票和概念日线 CSV。
  - `update_base_data/import_day_stock.py`: 将 CSV 导入 SQLite。
  - `update_base_data/update_etf.py`: ETF 数据采集。
  - `run_backtest.py`: 统一回测入口，支持 `stock` / `concept` / `etf`。
  - `daily_guide.py`: 每日候选和市场环境评估。
  - `daily_review.py`: 每日交易复盘报告。
  - `daily_full_flow.py`: 日终全流程。
  - `check_holdings_alert.py`: 持仓预警。
  - `alpha_factor_lab.py`, `concept_factor_lab.py`, `run_gtja_factor_lab.py`: 因子实验。
- `templates/`: Flask 页面模板。
- `data/`: 本地数据库、CSV、回测归档、交易日志、复盘报告等运行数据。
- `doc/`: 项目说明和研究资料。
- `logs/`: 日志输出。
- `杂七杂八/`: 与主项目无关的杂项资料，除非用户明确要求，不要修改。

## 运行环境

项目是普通 Python 工程，主要依赖见 `requirements.txt`：

```bash
pip install -r requirements.txt
```

启动 Web：

```bash
python main.py
```

常用脚本：

```bash
python script/update_base_data/update_daily.py
python script/update_base_data/import_day_stock.py -q
python script/run_backtest.py --list
python script/run_backtest.py --universe etf --strategy etf_alpha
python script/daily_review.py --save-only
python script/daily_guide.py
python script/check_holdings_alert.py
```

注意：部分历史注释或文档中的路径仍写作 `script/update_daily.py`、`script/import_day_stock.py`，当前实际文件在 `script/update_base_data/` 下。改动调度脚本时要核对真实路径。

## 数据与状态文件

- SQLite 数据库默认是 `data/stock.db`，由 `config.DATABASE_URL` 指定；`.gitignore` 已忽略 `data/*.db`。
- 股票日线 CSV: `data/day_stock/YYYYMM/YYYY-MM-DD.csv`。
- 概念日线 CSV: `data/day_concept/YYYYMM/YYYY-MM-DD.csv`。
- ETF 月度 CSV: `data/etf/YYYY/YYYY-MM.csv`。
- 概念月度 CSV: `data/concept/YYYY/YYYY-MM.csv`。
- 策略回测归档: `data/strategy/{strategy_id}/YYYY-MM/*.json`。
- 实盘组合和交易日志: `data/portfolio.json`, `data/trade_log.json`, `data/trading_journal.json`。
- 每日复盘: `data/reviews/YYYY-MM-DD.md`。
- QQ 推送配置: `data/qq_config.json`。

不要随意删除或重写 `data/` 下的运行数据。需要生成回测或更新数据时，优先让脚本追加/归档，避免覆盖用户已有结果。

## 策略接口约定

新增策略文件放在 `backtest/strategy/`，文件名必须形如 `strategy_xxx.py`，否则 `backtest/registry.py` 不会自动发现。

每个策略模块必须提供：

```python
META = {
    "id": "unique_id",
    "name": "显示名称",
    "description": "策略说明",
}

def generate_signals(bars):
    ...
```

`generate_signals` 返回信号列表，常见结构：

```python
{
    "date": "YYYY-MM-DD",
    "action": "buy" | "sell",
    "reason": "触发原因",
}
```

组合回测会按交易日处理信号。策略可选实现：

- `market_gate(date, market_stats)`: 返回是否允许买入及原因。
- `allow_buy(date, market_stats)`: 旧式市场过滤接口。

`portfolio.py` 会通过函数签名判断 `generate_signals` 是否接收 `market_stats`。修改策略接口前必须同时检查 `backtest/portfolio.py`、`script/run_backtest.py` 和 Web 回测 API。

## 回测与归档约定

- 单标的回测入口：`backtest.engine.run_backtest`。
- 组合回测入口：`backtest.portfolio.run_portfolio_backtest`。
- CLI 统一入口：`script/run_backtest.py`。
- Web 读取最新归档主要通过 `logic/backtest_cache.py` 和 `main.py` 中的 ETF 归档加载逻辑。
- 回测结果应保存到 `data/strategy/{strategy_id}/YYYY-MM/YYYY-MM-DD_序号.json`。

改策略后，至少运行：

```bash
python -m py_compile main.py backtest/registry.py backtest/engine.py backtest/portfolio.py script/run_backtest.py
python script/run_backtest.py --list
```

如果改了具体策略，再用对应 universe 跑一次小范围或默认回测，例如：

```bash
python script/run_backtest.py --universe etf --strategy etf_alpha
```

## Web/API 约定

主要页面：

- `/`: 首页。
- `/stocks`: 股票浏览。
- `/concepts`: 概念板块。
- `/strategy-backtest`: 策略回测。
- `/trading`: 实盘交易。
- `/trading-summary`: 交易汇总。
- `/strategy-lab`: 策略实验室。
- `/strategy-lab/detail`: 实验详情。

主要 API 包含：

- `/api/stocks`, `/api/stocks/<code>/daily`, `/api/stocks/prices`
- `/api/concepts`, `/api/concepts/<concept_code>/daily`, `/api/concepts/<concept_code>/stocks`
- `/api/backtest/strategies`, `/api/backtest/stock`, `/api/backtest/market/<strategy_id>`
- `/api/trading/state`, `/api/trading/buy`, `/api/trading/sell`, `/api/trading/journal`
- `/api/lab/*`
- `/api/download/*`

修改 API 返回结构前，必须同步检查 `templates/` 中对应页面的 JavaScript。

## 定时任务

`main.py` 内置后台线程，工作日 19:30 触发：

1. 更新股票和概念数据。
2. 导入 CSV 到 SQLite。
3. 运行日终全流程并推送。

调整调度逻辑时注意：

- `main.py` 当前使用 `subprocess` 调脚本。
- Windows 和 Linux 路径都可能被使用，优先用 `sys.executable` 和 `Path`。
- 不要在 Flask 请求线程里执行长时间同步任务，除非现有代码已经这样做且用户明确要求。

## 编码与文件风格

- 源码以 Python 为主，保持 UTF-8。
- 当前部分历史中文注释/文档显示为乱码，修改时不要扩大乱码范围；如果重写相关注释，请用正常 UTF-8 中文。
- 不要大规模格式化无关文件。
- 手工改代码时保持现有简单函数式风格，不引入新框架。
- ORM 表结构在 `models/stock.py`，改字段前要考虑已有 SQLite 数据兼容。

## 安全与敏感信息

- `config.py` 中包含同花顺 cookie，`data/qq_config.json` 可能包含 QQ 推送配置。不要把这些内容复制到日志、文档或对话输出中。
- 不要主动联网更新依赖或刷新 cookie，除非用户明确要求。
- 涉及交易建议、实盘买卖、推送内容时，保持“工具输出/策略信号”的表述，不要把回测结果包装成确定收益。

## 后续代理工作建议

接手任务时优先按这个顺序读代码：

1. 与请求直接相关的入口脚本或路由。
2. `backtest/registry.py` 和相关策略模块。
3. `backtest/portfolio.py` 的资金和信号处理逻辑。
4. `logic/backtest_cache.py` 或 `script/run_backtest.py` 的归档/加载逻辑。
5. 相关 `templates/` 页面。

常见风险点：

- 策略信号日期和成交日期是否存在未来函数。
- 数据源是 stock、concept 还是 etf，不能混用目录。
- 回测归档字段是否被 Web 页面依赖。
- 交易日志 JSON 的结构兼容性。
- 脚本路径是否仍引用历史位置。
