"""深度分析 etf_alpha042 策略"""
import json, os
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "data", "strategy", "etf_alpha")

# 找最新结果
latest = None
for root, dirs, files in os.walk(ARCHIVE):
    for f in sorted(files, reverse=True):
        if f.endswith(".json"):
            with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                latest = json.load(fh)
            break
    if latest:
        break

if not latest:
    print("未找到回测结果")
    exit()

trades = latest["trades"]
summary = latest["summary"]
curve = latest["equity_curve"]
stocks = latest["stock_summaries"]

print("=" * 70)
print("ETF Alpha042 策略深度分析")
print("=" * 70)

# 1. 交易分析
print("\n【1. 交易特征】")
print(f"  总交易: {len(trades)} 笔")
win_trades = [t for t in trades if t["profit"] > 0]
loss_trades = [t for t in trades if t["profit"] <= 0]
print(f"  盈利: {len(win_trades)} 笔, 亏损: {len(loss_trades)} 笔")

avg_win = sum(t["profit_pct"] for t in win_trades) / len(win_trades) if win_trades else 0
avg_loss = sum(t["profit_pct"] for t in loss_trades) / len(loss_trades) if loss_trades else 0
print(f"  平均盈利: {avg_win:+.1f}%, 平均亏损: {avg_loss:+.1f}%")
print(f"  盈亏比: {abs(avg_win/avg_loss):.2f}" if avg_loss else "")

# 持仓天数
hold_days = []
for t in trades:
    try:
        from datetime import datetime
        b = datetime.strptime(t["buy_date"], "%Y-%m-%d")
        s = datetime.strptime(t["sell_date"], "%Y-%m-%d")
        hold_days.append((s - b).days)
    except:
        pass
if hold_days:
    print(f"  持仓天数: 平均 {sum(hold_days)/len(hold_days):.0f}天, "
          f"中位数 {sorted(hold_days)[len(hold_days)//2]}天, "
          f"最长 {max(hold_days)}天")

# 卖出原因
sell_reasons = Counter()
for t in trades:
    reason = t.get("sell_reason", "")
    if "到期" in reason:
        sell_reasons["到期"] += 1
    elif "量价同步" in reason:
        sell_reasons["量价同步"] += 1
    elif "期末持仓" in reason:
        sell_reasons["期末持仓"] += 1
    else:
        sell_reasons["其他"] += 1
print(f"  卖出原因: {dict(sell_reasons)}")

# 到期 vs 量价同步 的盈亏对比
expire_trades = [t for t in trades if "到期" in t.get("sell_reason", "")]
sync_trades = [t for t in trades if "量价同步" in t.get("sell_reason", "")]
if expire_trades:
    avg_exp = sum(t["profit_pct"] for t in expire_trades) / len(expire_trades)
    print(f"  到期卖出: {len(expire_trades)}笔, 均盈{avg_exp:+.1f}%")
if sync_trades:
    avg_sync = sum(t["profit_pct"] for t in sync_trades) / len(sync_trades)
    print(f"  量价同步卖出: {len(sync_trades)}笔, 均盈{avg_sync:+.1f}%")

# 2. 年度分析
print("\n【2. 年度表现】")
yearly_trades = defaultdict(list)
yearly_equity = defaultdict(list)
for t in trades:
    yr = t["buy_date"][:4]
    yearly_trades[yr].append(t)
for p in curve:
    yr = p["date"][:4]
    yearly_equity[yr].append(p["equity"])

for yr in sorted(set(list(yearly_trades.keys()) + list(yearly_equity.keys()))):
    yt = yearly_trades.get(yr, [])
    ye = yearly_equity.get(yr, [])
    wins = sum(1 for t in yt if t["profit"] > 0)
    total_pnl = sum(t["profit"] for t in yt)
    if ye:
        ret = (ye[-1]/ye[0]-1)*100
    else:
        ret = 0
    print(f"  {yr}: {ret:>+6.1f}% | {len(yt):>3}笔 | 胜率{wins/max(1,len(yt))*100:.0f}% | 盈亏{total_pnl:>+10,.0f}")

# 3. 最大回撤分析
print("\n【3. 回撤分析】")
peak = 0
max_dd = 0
max_dd_start = ""
max_dd_end = ""
for p in curve:
    eq = p["equity"]
    if eq > peak:
        peak = eq
    dd = (eq - peak) / peak * 100
    if dd < max_dd:
        max_dd = dd
        if not max_dd_start:
            max_dd_start = p["date"]
        max_dd_end = p["date"]
    else:
        if max_dd < -5:
            pass  # 回撤恢复
        max_dd_start = ""

print(f"  最大回撤: {max_dd:.1f}% ({max_dd_start} ~ {max_dd_end})")

# 找重大回撤期
dd_periods = []
in_dd = False
dd_start = ""
dd_peak = 0
for p in curve:
    eq = p["equity"]
    if eq > dd_peak:
        dd_peak = eq
    dd = (eq - dd_peak) / dd_peak * 100 if dd_peak else 0
    if dd <= -10 and not in_dd:
        in_dd = True
        dd_start = p["date"]
    elif dd > -3 and in_dd:
        in_dd = False
        dd_periods.append((dd_start, p["date"], min((e["equity"]-dd_peak)/dd_peak*100
                            for e in curve if e["date"] >= dd_start and e["date"] <= p["date"])))
if dd_periods:
    print(f"  回撤>10%的时段:")
    for start, end, worst in dd_periods:
        print(f"    {start} ~ {end}: 最深{worst:.1f}%")

# 4. ETF 类型分析
print("\n【4. 分类表现】")
categories = {
    "宽基-沪深300": ["沪深300"],
    "宽基-中证500": ["中证500"],
    "宽基-中证1000": ["中证1000"],
    "宽基-创业板": ["创业板"],
    "宽基-科创50": ["科创50"],
    "宽基-其他": ["上证50", "A500", "综指", "中证2000", "深证100"],
    "行业-半导体": ["芯片", "半导体"],
    "行业-医药": ["医药", "医疗", "创新药", "中药"],
    "行业-证券": ["证券", "券商"],
    "行业-消费": ["酒", "消费", "食品", "家电"],
    "行业-TMT": ["通信", "5G", "计算机", "传媒", "游戏", "人工智能"],
    "行业-能源": ["煤炭", "电力", "新能源", "光伏", "有色"],
    "红利": ["红利", "低波"],
    "商品-黄金": ["黄金"],
}

for cat, kws in categories.items():
    cat_stocks = [s for s in stocks if any(kw in s["name"] for kw in kws)]
    if not cat_stocks:
        continue
    total_pnl = sum(s["profit"] for s in cat_stocks)
    total_tr = sum(s["trade_count"] for s in cat_stocks)
    total_w = sum(s["wins"] for s in cat_stocks)
    wr = total_w/max(1,total_tr)*100
    n = len(cat_stocks)
    print(f"  {cat:<16s}: {n:>2}只 盈亏{total_pnl:>+10,.0f}  {total_tr:>3}笔  胜率{wr:.0f}%")

# 5. 收益集中度
print("\n【5. 收益集中度】")
profits = sorted([s["profit"] for s in stocks], reverse=True)
total_profit = sum(p for p in profits if p > 0)
total_loss = abs(sum(p for p in profits if p < 0))
top3 = sum(profits[:3])
top5 = sum(profits[:5])
top10 = sum(profits[:10])
print(f"  TOP 3 贡献: {top3:,.0f} ({top3/total_profit*100:.0f}% of 总盈利)")
print(f"  TOP 5 贡献: {top5:,.0f} ({top5/total_profit*100:.0f}% of 总盈利)")
print(f"  TOP 10 贡献: {top10:,.0f} ({top10/total_profit*100:.0f}% of 总盈利)")

# 正负ETF数量
pos_etfs = [s for s in stocks if s["profit"] > 0]
neg_etfs = [s for s in stocks if s["profit"] <= 0]
print(f"  正收益ETF: {len(pos_etfs)}只, 总盈利+{sum(s['profit'] for s in pos_etfs):,.0f}")
print(f"  负收益ETF: {len(neg_etfs)}只, 总亏损{sum(s['profit'] for s in neg_etfs):,.0f}")

# 6. 市场择时效果
print("\n【6. 市场择时 (breadth>80% 禁买)】")
gate = latest.get("market_gate", {})
print(f"  允许开仓: {gate.get('allowed_days', '?')} 天")
print(f"  禁止开仓: {gate.get('blocked_days', '?')} 天")
print(f"  允许率: {gate.get('allowed_rate_pct', '?')}%")

# 择时的盈亏对比
gate_dates = {}
for g in gate.get("recent", []):
    gate_dates[g["date"]] = g.get("allowed", True)

# 检查禁止日是否避开了亏损
bad_day_trades = []
for t in trades:
    buy_date = t["buy_date"]
    # 检查是否在禁止日买入的（理论上不应该）
    if buy_date in gate_dates and not gate_dates[buy_date]:
        bad_day_trades.append(t)
if bad_day_trades:
    print(f"  [问题] {len(bad_day_trades)}笔交易在禁止日买入!")

# 7. 信号频率
print("\n【7. 信号密度】")
buy_trades = [t for t in trades if t.get("sell_reason") != "期末持仓"]
if buy_trades:
    # 按年统计信号
    buy_by_year = Counter()
    for t in buy_trades:
        buy_by_year[t["buy_date"][:4]] += 1
    for yr in sorted(buy_by_year):
        print(f"  {yr}: {buy_by_year[yr]}笔买入")
