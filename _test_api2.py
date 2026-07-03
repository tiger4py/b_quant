"""测试 ETF 策略详情 API"""
import urllib.request, json

for sid in ["etf_alpha", "etf_vegas"]:
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:8000/api/backtest/market/{sid}")
        data = json.loads(resp.read())
        if "error" in data:
            print(f"{sid}: 错误 - {data['error']}")
        else:
            s = data["summary"]
            print(f"{sid}: 收益={s['total_return_pct']}% 回撤={s['max_drawdown_pct']}% 交易={s['trade_count']}笔 权益曲线={len(data['equity_curve'])}点 OK")
    except Exception as e:
        print(f"{sid}: {e}")
