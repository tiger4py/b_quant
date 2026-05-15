import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backtest import get_strategy, run_portfolio_backtest
from config import DATABASE_URL
from logic.backtest_cache import load_market_bars
from models.stock import Base


def main():
    parser = argparse.ArgumentParser(description="Run market portfolio backtest for a strategy.")
    parser.add_argument("--strategy", required=True, help="strategy id")
    parser.add_argument("--days", type=int, default=1000, help="lookback trading days")
    parser.add_argument("--initial-cash", type=float, default=1000000, help="initial cash")
    parser.add_argument("--max-positions", type=int, default=5, help="max concurrent positions")
    parser.add_argument("--top", type=int, default=10, help="top stocks to print")
    args = parser.parse_args()

    engine = create_engine(DATABASE_URL, echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    strategy = get_strategy(args.strategy)
    with Session() as sess:
        stocks, bars_by_code, latest_date = load_market_bars(sess, args.days)

    if not stocks:
        raise ValueError("No market data available for backtest.")

    stock_map = {stock["code"]: stock for stock in stocks}
    result = run_portfolio_backtest(
        bars_by_code,
        stock_map,
        strategy,
        initial_cash=args.initial_cash,
        max_positions=args.max_positions,
    )
    summary = result["summary"]

    print(f"strategy={strategy.META['id']} name={strategy.META['name']}")
    print(
        f"stocks={len(stocks)} days={args.days} latest={latest_date} "
        f"initial={args.initial_cash:.2f} final={summary['final_equity']:.2f}"
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
