import argparse
from pathlib import Path
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from config import DATABASE_URL
from logic.backtest_cache import (
    DEFAULT_MARKET_INITIAL_CASH,
    DEFAULT_MARKET_MAX_POSITIONS,
    FIXED_START_DATE,
    compute_and_save_market_result,
)
from models.stock import Base


def run_strategy_meta(meta):
    parser = argparse.ArgumentParser(description=f"Run market backtest for strategy: {meta['id']}")
    parser.add_argument("--start", default=FIXED_START_DATE, help=f"start date (default {FIXED_START_DATE})")
    parser.add_argument("--end", default=None, help="end date YYYY-MM-DD (default: latest in DB)")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_MARKET_INITIAL_CASH, help="initial cash")
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MARKET_MAX_POSITIONS, help="max concurrent positions")
    parser.add_argument("--criteria", default="全部正常股票，剔除历史K线不足的股票", help="selection criteria label")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as sess:
        cache_key, result = compute_and_save_market_result(
            sess,
            strategy_id=meta["id"],
            start_date=args.start,
            end_date=args.end,
            initial_cash=args.initial_cash,
            max_positions=args.max_positions,
            criteria=args.criteria,
        )

    summary = result["summary"]
    selection = result["selection"]
    print(f"cache_key={cache_key}")
    print(f"strategy={meta['id']} name={meta['name']}")
    print(
        f"stocks={selection['stock_count']} start={selection['start_date']} end={selection['end_date']} "
        f"latest={selection['latest_trade_date']} initial={selection['initial_cash']:.2f} "
        f"final={summary['final_equity']:.2f} max_positions={selection['max_positions']}"
    )
    print(
        f"return={summary['total_return_pct']:.2f}% drawdown={summary['max_drawdown_pct']:.2f}% "
        f"win_rate={summary['win_rate_pct']:.2f}% trades={summary['trade_count']} "
        f"avg_profit={summary['avg_profit_pct']:.2f}% profit_factor={summary['profit_factor']}"
    )
