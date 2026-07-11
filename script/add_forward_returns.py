"""为 trade_log.json 中每条卖出预生成前向涨跌 HTML（含"至今"）"""
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily

TRADE_LOG = ROOT / 'data' / 'trade_log.json'
FORWARD_DAYS = [3, 5, 10, 20]


def get_forward_price(sess, code, sell_date_str, days):
    sell_date = datetime.strptime(sell_date_str, '%Y-%m-%d')
    target_date = sell_date + timedelta(days=days)
    target_str = target_date.strftime('%Y-%m-%d')
    row = (
        sess.query(StockDaily.close, StockDaily.trade_date)
        .filter(StockDaily.code == code)
        .filter(StockDaily.trade_date >= target_str)
        .order_by(StockDaily.trade_date)
        .first()
    )
    if row:
        return row.close, row.trade_date
    return None, None


def fwd_html_cell(chg_pct):
    if chg_pct is None:
        return '<td style="font-size:10px;color:#374151">-</td>'
    sign = "+" if chg_pct >= 0 else ""
    cls = "g" if chg_pct >= 0 else "r"
    return f'<td class="{cls}" style="font-size:11px">{sign}{chg_pct:.1f}%</td>'


def main():
    with open(TRADE_LOG, 'r', encoding='utf-8') as f:
        logs = json.load(f)

    sells = [l for l in logs if l['action'] == 'sell']
    print(f'Computing forward for {len(sells)} sells...')

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    # 取最新的交易日期
    latest_date = sess.query(func.max(StockDaily.trade_date)).scalar()
    print(f'Latest DB date: {latest_date}')

    for i, s in enumerate(sells):
        code = s['code']
        sell_date = s['date']
        sell_price = s['price']

        # 前向 N 天
        fwd = {}
        html_parts = []
        for d in FORWARD_DAYS:
            fp, fd = get_forward_price(sess, code, sell_date, d)
            if fp and sell_price > 0:
                chg_pct = round((fp / sell_price - 1) * 100, 2)
                fwd[f'fwd_{d}d'] = chg_pct
                fwd[f'fwd_{d}d_date'] = fd
            else:
                fwd[f'fwd_{d}d'] = None
                fwd[f'fwd_{d}d_date'] = None
            html_parts.append(fwd_html_cell(fwd[f'fwd_{d}d']))

        # "至今" — 最新价 vs 卖出价
        now_row = (
            sess.query(StockDaily.close, StockDaily.trade_date)
            .filter(StockDaily.code == code)
            .filter(StockDaily.trade_date == latest_date)
            .first()
        )
        if now_row and sell_price > 0:
            now_chg = round((now_row.close / sell_price - 1) * 100, 2)
        else:
            now_chg = None
        html_parts.append(fwd_html_cell(now_chg))
        fwd['fwd_now'] = now_chg

        s['forward'] = fwd
        s['fwdHtml'] = ''.join(html_parts)  # 5 个 <td>: 3d,5d,10d,20d,至今

        if (i + 1) % 20 == 0:
            print(f'  {i+1}/{len(sells)}...')

    sess.close()

    with open(TRADE_LOG, 'w', encoding='utf-8') as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

    print(f'Done. {len(sells)} sells updated.')

    # Show samples
    for s in sells[:2]:
        print(f'  {s["date"]} {s["name"]}: fwdHtml={s["fwdHtml"]}')
    # Show a recent one too
    recent = sorted(sells, key=lambda x: x['date'], reverse=True)[0]
    print(f'  {recent["date"]} {recent["name"]}: fwdHtml={recent["fwdHtml"]}')


if __name__ == '__main__':
    main()
