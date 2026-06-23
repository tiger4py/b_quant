#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓平仓线实时检查 + QQ推送

用法:
  python script/check_holdings_alert.py          # 检查打印
  python script/check_holdings_alert.py --push   # 检查 + QQ推送
  python script/check_holdings_alert.py --loop   # 循环监控 (每60秒)
"""
import sys, json, argparse, urllib.request, time
from pathlib import Path
from datetime import datetime

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily, StockInfo
from backtest.indicators import sma

STOP_LOSS_PCT = -8
HIGH_RETREAT_PCT = -10
VOL_COLLAPSE_RATIO = 0.7
VOL_DIVERGE_RATIO = 1.0

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)


def fetch_live_prices(codes):
    """
    从新浪获取实时价格。
    codes: ["sz.002378", "sh.600596", ...]
    返回: {code: {name, price, open, high, low, volume, amount, date}}
    """
    # 转成新浪格式: sh600596, sz002378
    sina_codes = [c.replace(".", "") for c in codes]
    url = "http://hq.sinajs.cn/list=" + ",".join(sina_codes)

    req = urllib.request.Request(url)
    req.add_header("Referer", "https://finance.sina.com.cn")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        data = resp.read().decode("gbk")
    except Exception as e:
        print(f"[!] 获取实时行情失败: {e}")
        return {}

    result = {}
    for line in data.strip().split("\n"):
        if not line.strip() or "=" not in line:
            continue
        try:
            # var hq_str_sz002378="name,open,prev_close,price,high,low,..."
            code_part, quote_part = line.split("=", 1)
            sina_code = code_part.replace("var hq_str_", "").strip()
            # 转回 b_quant 格式
            code = sina_code[:2] + "." + sina_code[2:]
            fields = quote_part.strip('";').split(",")
            if len(fields) < 10:
                continue
            result[code] = {
                "name": fields[0],
                "open": float(fields[1]) if fields[1] else 0,
                "prevClose": float(fields[2]) if fields[2] else 0,
                "price": float(fields[3]) if fields[3] else 0,
                "high": float(fields[4]) if fields[4] else 0,
                "low": float(fields[5]) if fields[5] else 0,
                "volume": float(fields[8]) if fields[8] else 0,
                "amount": float(fields[9]) if fields[9] else 0,
                "date": fields[30] if len(fields) > 30 else "",
            }
        except Exception as e:
            print(f"[!] 解析失败: {line[:60]}... {e}")
    return result


def check_all():
    """检查所有持仓"""
    sess = Session()

    # 持仓
    portfolio_file = ROOT_DIR / "data" / "portfolio.json"
    if not portfolio_file.exists():
        print("无持仓文件"); return None, None, None
    with open(portfolio_file, encoding="utf-8") as f:
        portfolio = json.load(f)
    holdings = portfolio.get("holdings", [])
    if not holdings:
        print("当前无持仓"); return None, None, None

    # 实时价格
    codes = [h["code"] for h in holdings]
    live = fetch_live_prices(codes)
    if not live:
        print("[!] 无法获取实时行情，终止检查")
        return None, None, None

    # DB 历史数据(用于MA/量计算)
    db_date = sess.query(func.max(StockDaily.trade_date)).scalar()

    alerts = []
    print(f"数据库最新: {db_date}  |  实时行情: {list(live.values())[0].get('date','?') if live else '?'}")
    print()

    for h in holdings:
        code = h["code"]
        name = h["name"]
        buy_price = h["buy_price"]
        buy_date = h["buy_date"]
        shares = h["shares"]

        lv = live.get(code)
        if not lv:
            print(f"  {code} {name}: 无实时数据"); continue

        realtime_price = lv["price"]
        realtime_vol = lv["volume"]

        # DB历史K线
        rows = (
            sess.query(StockDaily)
            .filter(StockDaily.code == code)
            .order_by(StockDaily.trade_date.desc())
            .limit(45)
            .all()
        )
        rows.reverse()
        if len(rows) < 20:
            continue

        closes = [r.close for r in rows]
        highs = [r.high for r in rows]
        volumes = [r.volume or 0 for r in rows]

        # 用实时价替换最后一天的收盘价来算均线
        closes_for_ma = closes[:-1] + [realtime_price]
        highs_for_peak = highs[:-1] + [max(highs[-1], lv["high"])]

        n = len(closes_for_ma)
        i = n - 1

        ma10 = sma(closes_for_ma, 10)
        ma20 = sma(closes_for_ma, 20)

        # 量比：用DB的5日/20日均量（实时量是当天累计，不可直接比均量）
        vol_ma5 = sma(volumes, 5)
        vol_ma20_arr = sma(volumes, 20)
        vr = vol_ma5[len(volumes)-1] / vol_ma20_arr[len(volumes)-1] if vol_ma20_arr[len(volumes)-1] > 0 else 0

        # 持仓高点
        buy_idx = None
        for j in range(len(rows)):
            if rows[j].trade_date >= buy_date:
                buy_idx = j; break
        if buy_idx is None:
            buy_idx = max(0, len(rows) - 5)
        peak_hist = max(highs[buy_idx:])
        peak = max(peak_hist, lv["high"])

        stop_loss = round(buy_price * (1 + STOP_LOSS_PCT / 100), 2)
        retreat = round(peak * (1 + HIGH_RETREAT_PCT / 100), 2)
        pnl_pct = round((realtime_price / buy_price - 1) * 100, 1)
        day_chg = round((realtime_price / lv["prevClose"] - 1) * 100, 1) if lv["prevClose"] else 0

        triggered = []
        if realtime_price <= stop_loss:
            triggered.append(f"🔴 止损: {realtime_price:.2f} <= {stop_loss} (买入{buy_price})")
        if realtime_price <= retreat:
            triggered.append(f"🔴 高位回撤-10%: {realtime_price:.2f} <= {retreat} (高点{peak:.2f})")
        if ma10[i] and realtime_price <= ma10[i]:
            triggered.append(f"🟡 破MA10: {realtime_price:.2f} <= {ma10[i]:.2f}")
        if ma20[i] and realtime_price <= ma20[i]:
            triggered.append(f"🟡 破MA20: {realtime_price:.2f} <= {ma20[i]:.2f}")
        if vr < VOL_COLLAPSE_RATIO:
            triggered.append(f"🔴 量能崩塌: 量比{vr:.2f} < 0.7")
        if vr < VOL_DIVERGE_RATIO and realtime_price > buy_price:
            triggered.append(f"🟡 量价背离: 量比{vr:.2f} < 1.0")

        status = "🔴 平仓!" if any("🔴" in t for t in triggered) else ("🟡 预警" if triggered else "✅")
        print(f"  {code} {name}  实时{realtime_price:.2f}({day_chg:+.1f}%)  盈亏{pnl_pct:+.1f}%  止损{stop_loss}  回撤{retreat}  MA10:{ma10[i]:.2f}  量比{vr:.2f}  {status}")
        for t in triggered:
            print(f"    {t}")

        if triggered:
            alerts.append({
                "code": code, "name": name, "price": realtime_price, "pnlPct": pnl_pct,
                "shares": shares, "buyPrice": buy_price, "triggered": triggered,
                "stopLoss": stop_loss, "retreat": retreat, "ma10": ma10[i], "status": status,
            })

    sess.close()
    return db_date, alerts, live


def build_qq_message(db_date, alerts, live):
    lines = [
        f"[持仓预警] 实时检查",
        f"DB日期:{db_date}  |  共{len(alerts)}只触发平仓/预警线",
        "",
    ]
    for a in alerts:
        lines.append(f"{a['status']} {a['name']}({a['code']})  现价{a['price']:.2f}")
        lines.append(f"  止损{a['stopLoss']}  回撤{a['retreat']}  MA10:{a['ma10']:.2f}")
        for t in a["triggered"]:
            lines.append(f"  {t}")
        lines.append("")
    lines.append("--- 趋势跟随 · 实时平仓预警 ---")
    return "\n".join(lines)


def push_qq(msg):
    try:
        from models.qq_webhook import QQPusher
        pusher = QQPusher()
        if pusher.enabled:
            result = pusher.push_long_text(msg)
            print(f"\n[QQ] 推送完成: success={result['success']}, fail={result['fail']}")
        else:
            print("\n[QQ] 推送未启用")
    except Exception as e:
        print(f"\n[QQ] 推送异常: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="持仓平仓线实时检查")
    parser.add_argument("--push", action="store_true", help="推送到QQ")
    parser.add_argument("--loop", action="store_true", help="循环监控(每60秒)")
    parser.add_argument("--interval", type=int, default=60, help="循环间隔秒数(默认60)")
    args = parser.parse_args()

    if args.loop:
        print(f"循环监控模式，每{args.interval}秒检查一次，Ctrl+C 停止\n")

        # 每日推送记录: {"2026-06-23": ["sz.002378:高位回撤", ...]}
        alert_log_file = ROOT_DIR / "data" / "alert_history.json"
        def load_pushed():
            if alert_log_file.exists():
                with open(alert_log_file, encoding="utf-8") as f:
                    return json.load(f)
            return {}
        def save_pushed(data):
            with open(alert_log_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        while True:
            try:
                db_date, alerts, live = check_all()
                today = datetime.now().strftime("%Y-%m-%d")
                pushed = load_pushed()
                today_pushed = set(pushed.get(today, []))

                if alerts:
                    # 过滤：今天还没推送过的
                    new_alerts = []
                    for a in alerts:
                        keys = [f"{a['code']}:{t.split(chr(10))[0][:8]}" for t in a["triggered"]]
                        if not any(k in today_pushed for k in keys):
                            new_alerts.append(a)
                            for k in keys:
                                today_pushed.add(k)

                    if new_alerts:
                        # 只推新增的
                        msg = build_qq_message(db_date, new_alerts, live)
                        print(f"\n--- 新增预警 ---\n{msg}")
                        if args.push:
                            push_qq(msg)
                        pushed[today] = list(today_pushed)
                        save_pushed(pushed)
                    else:
                        t = datetime.now().strftime("%H:%M:%S")
                        print(f"  [{t}] 预警已推送过，跳过")
                else:
                    t = datetime.now().strftime("%H:%M:%S")
                    print(f"  [{t}] ✅ 正常")
            except Exception as e:
                print(f"[!] 检查异常: {e}")
            time.sleep(args.interval)
    else:
        db_date, alerts, live = check_all()
        if alerts:
            print(f"\n⚠️ 触发预警: {len(alerts)} 只")
            msg = build_qq_message(db_date, alerts, live)
            print(f"\n--- 预警消息 ---\n{msg}")
            if args.push:
                push_qq(msg)
        else:
            print("\n✅ 所有持仓正常，无平仓触发")
