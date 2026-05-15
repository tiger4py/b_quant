import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config import DATABASE_URL
from logic.backtest_cache import (
    MACD_MARKET_CACHE_KEY,
    compute_macd_market_result,
    save_backtest_cache,
)
from models.stock import Base


def main():
    engine = create_engine(DATABASE_URL, echo=False)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as sess:
        result = compute_macd_market_result(
            sess,
            days=1000,
            initial_cash=1000000,
            max_positions=5,
        )
        save_backtest_cache(sess, MACD_MARKET_CACHE_KEY, result)
        summary = result["summary"]
        selection = result["selection"]
        print("MACD market backtest cached")
        print(f"stocks={selection['stock_count']} days={selection['days']} latest={selection['latest_trade_date']}")
        print(f"return={summary['total_return_pct']}% final={summary['final_equity']} trades={summary['trade_count']}")


if __name__ == "__main__":
    main()
