"""测试 ETF 策略 API"""
import urllib.request, json

# 列出策略
resp = urllib.request.urlopen("http://127.0.0.1:8000/api/backtest/strategies")
data = json.loads(resp.read())
print("=== 所有策略 ===")
for s in data:
    tag = "[ETF]" if s.get("type") == "etf" else "[STOCK]"
    print(f"  {tag} {s['id']:25s} {s['name']}")

# overview
resp2 = urllib.request.urlopen("http://127.0.0.1:8000/api/backtest/market-overview")
data2 = json.loads(resp2.read())
print(f"\n=== 排名 ({len(data2['ranking'])} 个有结果, {len(data2['missing'])} 个缺失) ===")
for r in data2["ranking"]:
    print(f"  {r['strategy_id']:25s} {r['return_pct']:>+7.1f}%  回撤{r['drawdown_pct']:>5.1f}%  {r['trade_count']:>4}笔  评分{r['score']:.1f}")
if data2["missing"]:
    print(f"\n=== 缺失 ===")
    for m in data2["missing"]:
        print(f"  {m['strategy_id']}: {m['strategy_name']}")

# etf_alpha 详情
resp3 = urllib.request.urlopen("http://127.0.0.1:8000/api/backtest/market/etf_alpha")
data3 = json.loads(resp3.read())
if "error" in data3:
    print(f"\nETF Alpha 错误: {data3['error']}")
else:
    print(f"\n=== ETF Alpha 详情 ===")
    print(f"  收益: {data3['summary']['total_return_pct']}%")
    print(f"  回撤: {data3['summary']['max_drawdown_pct']}%")
    print(f"  权益曲线: {len(data3['equity_curve'])} 点")
    print(f"  交易: {len(data3['trades'])} 笔")
