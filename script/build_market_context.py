"""清空持仓 + 计算每个交易日的大盘环境 + 领涨/领跌概念"""
import json
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, Concept, ConceptDaily

TRADE_LOG = ROOT / 'data' / 'trade_log.json'
MARKET_CTX = ROOT / 'data' / 'market_context.json'


def build_market_context():
    with open(TRADE_LOG, 'r', encoding='utf-8') as f:
        logs = json.load(f)
    trade_dates = sorted(set(l['date'] for l in logs))
    print(f'Trading days: {len(trade_dates)}')

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    # Pre-load concept name mapping
    concept_map = {}
    for c in sess.query(Concept).all():
        concept_map[c.code] = c.name

    market_data = {}
    for date in trade_dates:
        # --- Stock breadth ---
        rows = sess.query(
            StockDaily.close, StockDaily.open, StockDaily.volume
        ).filter(StockDaily.trade_date == date).all()

        up = down = flat = 0
        chgs = []
        for r in rows:
            if r.open and r.close and r.open > 0:
                pct = (r.close - r.open) / r.open * 100
                chgs.append(pct)
                if pct > 0.01:
                    up += 1
                elif pct < -0.01:
                    down += 1
                else:
                    flat += 1

        total = len(rows)
        avg_chg = sum(chgs) / len(chgs) if chgs else 0
        up_ratio = up / total * 100 if total else 0
        limit_up = sum(1 for c in chgs if c >= 9.5)
        limit_down = sum(1 for c in chgs if c <= -9.5)

        if up_ratio >= 65:
            sentiment = "GREEN"
        elif up_ratio <= 40:
            sentiment = "RED"
        else:
            sentiment = "YELLOW"

        # --- Concept leaders/laggards ---
        c_rows = sess.query(
            ConceptDaily.concept_code, ConceptDaily.close, ConceptDaily.open
        ).filter(ConceptDaily.trade_date == date).all()

        concept_chgs = []
        for cr in c_rows:
            if cr.open and cr.close and cr.open > 0:
                cpct = (cr.close - cr.open) / cr.open * 100
                name = concept_map.get(cr.concept_code, cr.concept_code)
                concept_chgs.append((name, round(cpct, 2)))

        concept_chgs.sort(key=lambda x: -x[1])
        top_concepts = concept_chgs[:5]   # 领涨
        bot_concepts = concept_chgs[-5:]  # 领跌（反转）

        # Build description
        desc_parts = []
        if up_ratio > 65:
            desc_parts.append("普涨")
        elif up_ratio > 45:
            desc_parts.append("分化")
        else:
            desc_parts.append("普跌")

        if top_concepts:
            desc_parts.append("领涨:" + ",".join(f"{n}{v:+.1f}%" for n, v in top_concepts[:3]))
        if bot_concepts:
            desc_parts.append("领跌:" + ",".join(f"{n}{v:+.1f}%" for n, v in bot_concepts[:3]))
        desc_parts.append(f"[{sentiment}]")

        market_data[date] = {
            "date": date,
            "total_stocks": total,
            "up": up, "down": down, "flat": flat,
            "up_ratio": round(up_ratio, 1),
            "avg_pct": round(avg_chg, 3),
            "limit_up": limit_up, "limit_down": limit_down,
            "sentiment": sentiment,
            "top_concepts": [{"name": n, "pct": v} for n, v in top_concepts],
            "bot_concepts": [{"name": n, "pct": v} for n, v in reversed(bot_concepts)],
            "description": " ".join(desc_parts),
        }

    sess.close()

    with open(MARKET_CTX, 'w', encoding='utf-8') as f:
        json.dump(market_data, f, ensure_ascii=False, indent=2)
    print(f'Saved market context: {len(market_data)} days')

    # Print summary
    for date in sorted(market_data):
        m = market_data[date]
        print(f'  {date}: {m["description"]}')


if __name__ == '__main__':
    build_market_context()
