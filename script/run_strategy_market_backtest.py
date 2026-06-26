"""全市场回测脚本 — 指定日期范围，结果存文件"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backtest import get_strategy
from config import DATABASE_URL
from logic.backtest_cache import (
    DEFAULT_MARKET_INITIAL_CASH,
    DEFAULT_MARKET_MAX_POSITIONS,
    FIXED_START_DATE,
    compute_and_save_market_result,
)
from models.stock import Base


def main():
    parser = argparse.ArgumentParser(description="Run market portfolio backtest for a strategy.")
    parser.add_argument("--strategy", required=True, help="strategy id")
    parser.add_argument("--start", default=FIXED_START_DATE, help=f"start date YYYY-MM-DD (default {FIXED_START_DATE})")
    parser.add_argument("--end", default=None, help="end date YYYY-MM-DD (default: latest in DB)")
    parser.add_argument("--initial-cash", type=float, default=DEFAULT_MARKET_INITIAL_CASH, help="initial cash")
    parser.add_argument("--max-positions", type=int, default=DEFAULT_MARKET_MAX_POSITIONS, help="max concurrent positions")
    parser.add_argument("--top", type=int, default=10, help="top stocks to print")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    strategy = get_strategy(args.strategy)
    with Session() as sess:
        cache_key, result = compute_and_save_market_result(
            sess,
            strategy_id=args.strategy,
            start_date=args.start,
            end_date=args.end,
            initial_cash=args.initial_cash,
            max_positions=args.max_positions,
            criteria="全部正常股票，剔除历史K线不足的股票",
        )
    selection = result["selection"]
    summary = result["summary"]

    print(f"cache_key={cache_key}")
    print(f"strategy={strategy.META['id']} name={strategy.META['name']}")
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
    print("top stocks:")
    for item in result["stock_summaries"][: args.top]:
        print(
            f"{item['code']} {item['name']} profit={item['profit']:.2f} "
            f"trades={item['trade_count']} win_rate={item['win_rate_pct']:.2f}%"
        )


if __name__ == "__main__":
    main()
