import urllib.request, json
resp = urllib.request.urlopen("http://127.0.0.1:8000/api/backtest/market/etf_alpha/analysis")
d = json.loads(resp.read())

tp = d["trade_profile"]
print(f"交易: 总{tp['total']}笔 盈{tp['wins']}/亏{tp['losses']} 盈亏比{tp['wl_ratio']}")
print(f"持仓: 均{tp['avg_hold']}天 中{tp['med_hold']}天 长{tp['max_hold']}天")
print(f"卖出: {tp['sell_reasons']}")
print()
print("年度 TOP 5:")
for y in d["yearly"]:
    print(f"  {y['year']}: {y['return_pct']:>+5.1f}% {y['trades']:>3}笔 wr={y['win_rate']}%")
print()
print("分类:")
for c in d["categories"][:5]:
    print(f"  {c['name']}: {c['count']}只 盈亏{c['profit']:+,.0f} 胜率{c['win_rate']}%")
cc = d["concentration"]
print(f"\n集中度: 正{cc['pos_etfs']}只/负{cc['neg_etfs']}只 TOP3贡献{cc['top3_pct']}%")
print(f"\n✅ API OK! 打开 http://127.0.0.1:8000/strategy-backtest")
