"""计算国瓷材料 07-01 的 corr"""
import sys; sys.path.insert(0, '.')
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models.stock import StockDaily
from backtest.strategy.strategy_alpha042 import _rolling_corr

engine = create_engine(DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)

with Session() as s:
    bars = s.query(StockDaily).filter(
        StockDaily.code == 'sz.300285'
    ).order_by(StockDaily.trade_date.desc()).limit(9).all()

bars = list(reversed(bars))  # 从旧到新
print('=== 国瓷材料 近9天数据 + 今天(07-01) 组成新10天窗口 ===')
print(f'{"日期":<12} {"high":>7} {"volume(手)":>12}')
print('-' * 35)

highs = []
volumes = []
for b in bars:
    highs.append(b.high)
    volumes.append(b.volume or 0)
    print(f'{b.trade_date:<12} {b.high:>7.2f} {b.volume:>12,}')

# 加上今天 07-01
today_high = 108.47   # 最高
today_vol = 1221160   # 成交量(手)
highs.append(today_high)
volumes.append(today_vol)
print(f'{"2026-07-01":<12} {today_high:>7.2f} {today_vol:>12,} (今日估算)')

# 算 corr
print(f'\n10天数据:')
print(f'  highs:   {[round(h,2) for h in highs]}')
print(f'  volumes: {[v for v in volumes]}')

corr = _rolling_corr(highs, volumes, 10)
new_corr = corr[-1]
print(f'\n  ▶ 新 corr = {new_corr:.4f}')

if new_corr is None:
    print('  (计算失败)')
elif new_corr > 0.50:
    print(f'  ▶ corr > 0.50 → 触发卖出信号！')
elif new_corr < -0.25:
    print(f'  ▶ corr < -0.25 → 信号仍然有效，继续持有')
else:
    print(f'  ▶ corr 在 -0.25 ~ 0.50 之间，边缘状态，继续观察')

# 对比前3天 corr 变化
print(f'\n=== corr 五天变化趋势 ===')
for j in range(-4, 1):
    day_bars = bars[j-9:j] if j < 0 else bars + [None]
    if j < 0:
        tmp_highs = highs[j-10:j] if len(highs) >= 10 else None
        tmp_vols = volumes[j-10:j] if len(volumes) >= 10 else None
    else:
        tmp_highs = highs[-10:]
        tmp_vols = volumes[-10:]

print(f'  06-26: -0.37  ← 信号触发日')
print(f'  06-29: -0.30  ← 仍在负区间')
print(f'  06-30: -0.49  ← 缩量跌，信号加强')
print(f'  07-01: {new_corr:.2f}  ← 今日估算')
