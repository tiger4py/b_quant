import json
from pathlib import Path

ROOT = Path(r'd:\my_import\sync_content\code\b_quant')

for sid in ["etf_alpha", "etf_vegas"]:
    d = ROOT / "data" / "strategy" / sid
    if not d.exists():
        print(f"{sid}: 无归档")
        continue
    files = list(d.rglob("*.json"))
    print(f"{sid}: {len(files)} 个归档")
    if files:
        latest = max(files, key=lambda f: f.stat().st_mtime)
        print(f"  最新: {latest.name}")
        with open(latest, "r", encoding="utf-8") as f:
            data = json.load(f)
        s = data["summary"]
        print(f"  收益={s['total_return_pct']}% 回撤={s['max_drawdown_pct']}% 胜率={s['win_rate_pct']}%")
        print(f"  权益={len(data['equity_curve'])}点 个股={len(data['stock_summaries'])}只 交易={len(data['trades'])}笔")
