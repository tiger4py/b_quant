# 策略评估分析 Skill（增强版）

对任意回测策略做完整的 **分析→诊断→修复→重跑→对比** 循环。

## 触发条件

用户说"分析策略"、"评估策略"、"诊断策略"、"review strategy"、"帮我看看这个策略"、"为什么这个策略不赚钱"时触发。

## 完整工作流（5 步闭环）

```
分析结果 → 诊断根因 → 提方案 → 修代码 → 重跑对比
   ↑                                          │
   └──────────── 循环直到满意 ←────────────────┘
```

---

## 第 1 步：快速概览（2 分钟）

从最新 archive JSON 提取关键指标，判断策略是否"活了"：

```python
import json
from pathlib import Path

def quick_overview(strategy_id):
    """加载最新结果，输出核心指标"""
    root = Path('data/strategy') / strategy_id
    month_dirs = sorted([d for d in root.iterdir() if d.is_dir()], 
                        key=lambda d: d.name, reverse=True)
    if not month_dirs:
        return None
    files = sorted(month_dirs[0].glob("*.json"), 
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None
    
    with open(files[0], 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    s = data['summary']
    print(f"策略: {data['strategy']['name']}")
    print(f"收益: {s['total_return_pct']:.2f}%  回撤: {s['max_drawdown_pct']:.2f}%")
    print(f"胜率: {s['win_rate_pct']:.1f}%  交易: {s['trade_count']}笔")
    print(f"平均盈利: {s['avg_profit_pct']:.2f}%  盈亏比: {s['profit_factor']}")
    print(f"区间: {data['selection']['start_date']} ~ {data['selection']['end_date']}")
    return data
```

**判断标准**：
- 交易数 = 0 → **策略根本没触发**，检查 market_gate 或信号逻辑
- 交易数 > 0 但收益 < 0 → **策略逻辑有问题**，进入诊断
- 收益 > 0 但 PF < 1.2 → **靠运气**，胜率不够或盈亏比差
- 收益 > 20% 且 PF > 1.5 → **策略有效**，可以微调优化

---

## 第 2 步：诊断分析（跑 5 个维度）

### 2.1 卖出原因拆解（找"凶手"）

```python
from collections import Counter

def analyze_sell_reasons(data):
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    
    # 分类
    def classify(reason):
        if '止损' in reason: return '止损'
        if '移动止盈' in reason or '止盈' in reason: return '移动止盈'
        if '到期' in reason: return '到期'
        return '其他'
    
    groups = {}
    for t in trades:
        cls = classify(t['sell_reason'])
        g = groups.setdefault(cls, {'count': 0, 'wins': 0, 'profit': 0.0, 'pcts': []})
        g['count'] += 1
        g['profit'] += t['profit']
        g['pcts'].append(t['profit_pct'])
        if t['profit'] > 0:
            g['wins'] += 1
    
    print(f"\n{'卖出类型':<10} {'笔数':>4} {'占比':>6} {'胜率':>6} {'累计盈亏':>12} {'平均%':>7}")
    print('-' * 55)
    for cls in ['止损', '移动止盈', '到期']:
        g = groups.get(cls)
        if not g: continue
        print(f"{cls:<10} {g['count']:>4} {g['count']/len(trades)*100:>5.1f}% "
              f"{g['wins']/g['count']*100:>5.0f}% {g['profit']:>12,.0f} "
              f"{sum(g['pcts'])/len(g['pcts']):>6.1f}%")
    
    # 止损组里找跳空穿止损的（亏损远超止损线）
    big_losers = [t for t in trades if '止损' in t.get('sell_reason','') and t['profit_pct'] < -15]
    if big_losers:
        print(f"\n跳空穿止损({len(big_losers)}笔):")
        for t in sorted(big_losers, key=lambda x: x['profit_pct'])[:5]:
            print(f"  {t['name']} {t['buy_date']}→{t['sell_date']} {t['profit_pct']:.1f}%")
    
    return groups
```

### 2.2 年度收益分布（看市场环境适应性）

```python
from collections import defaultdict

def analyze_by_year(data):
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    by_year = defaultdict(lambda: {'count':0, 'wins':0, 'profit':0.0, 'pcts':[]})
    
    for t in trades:
        year = t['buy_date'][:4]
        y = by_year[year]
        y['count'] += 1
        y['profit'] += t['profit']
        y['pcts'].append(t['profit_pct'])
        if t['profit'] > 0:
            y['wins'] += 1
    
    print(f"\n{'年份':<6} {'笔数':>4} {'胜率':>6} {'累计盈亏':>12} {'平均%':>7} {'最佳%':>7} {'最差%':>7}")
    print('-' * 60)
    for year in sorted(by_year):
        y = by_year[year]
        print(f"{year:<6} {y['count']:>4} {y['wins']/y['count']*100:>5.0f}% "
              f"{y['profit']:>12,.0f} {sum(y['pcts'])/len(y['pcts']):>6.1f}% "
              f"{max(y['pcts']):>6.1f}% {min(y['pcts']):>6.1f}%")
    
    return by_year
```

**关键判断**：
- 牛熊年收益分化大 → 策略依赖市场环境
- 连续 2 年亏损 → 策略逻辑有根本问题
- 每年稳定正收益 → 策略鲁棒性强

### 2.3 盈亏不对称性（看是否少数大赢家撑起全部）

```python
def analyze_pnl_asymmetry(data):
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    winners = [t for t in trades if t['profit'] > 0]
    losers = [t for t in trades if t['profit'] <= 0]
    
    if winners:
        print(f"\n盈利交易: {len(winners)}笔, 平均+{sum(t['profit_pct'] for t in winners)/len(winners):.1f}%")
        # Top 5 贡献了多少利润
        top5 = sorted(winners, key=lambda x: -x['profit'])[:5]
        top5_profit = sum(t['profit'] for t in top5)
        total_profit = sum(t['profit'] for t in winners)
        print(f"Top 5 赢家贡献: {top5_profit:,.0f} / {total_profit:,.0f} = {top5_profit/total_profit*100:.0f}%")
        for t in top5:
            print(f"  {t['name']} +{t['profit_pct']:.1f}% ({t['buy_date']}→{t['sell_date']})")
    
    if losers:
        print(f"\n亏损交易: {len(losers)}笔, 平均{sum(t['profit_pct'] for t in losers)/len(losers):.1f}%")
```

**关键判断**：如果 Top 5 贡献 > 60% 利润 → 策略极度依赖少数大赢家，样本量不够时要警惕过拟合。

### 2.4 卖出后追踪（**最重要的分析**）

对每笔已平仓交易，查卖出后 1/2/3/4/5/6 个月的股价，判断是否卖早了：

```python
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from models.stock import StockDaily
from config import DATABASE_URL

def forward_analysis(data):
    """查每笔交易卖出后 1-6 个月的涨跌"""
    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()
    
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    
    results = []
    for t in trades:
        sell_date = datetime.strptime(t['sell_date'], '%Y-%m-%d')
        fwd_returns = []
        for months in [1, 2, 3, 4, 5, 6]:
            target = sell_date
            for _ in range(months):
                # 加一个月
                m = target.month + 1
                y = target.year + (m - 1) // 12
                m = (m - 1) % 12 + 1
                target = datetime(y, m, min(target.day, 28))
            
            row = sess.query(StockDaily).filter(
                StockDaily.code == t['code'],
                StockDaily.trade_date >= target.strftime('%Y-%m-%d')
            ).order_by(StockDaily.trade_date).first()
            
            if row and t['sell_price'] > 0:
                fwd_returns.append((row.close / t['sell_price'] - 1) * 100)
            else:
                fwd_returns.append(None)
        
        results.append({**t, 'fwd': fwd_returns})
    
    sess.close()
    
    # 按卖出类型统计
    def classify(reason):
        if '止损' in reason: return '止损'
        if '移动' in reason: return '移动止盈'
        if '到期' in reason: return '到期'
        return '其他'
    
    by_type = defaultdict(list)
    for r in results:
        by_type[classify(r['sell_reason'])].append(r)
    
    print(f"\n{'卖出类型':<10} {'笔数':>4}", end='')
    for m in range(1, 7):
        print(f" {'{}月后'.format(m):>8}", end='')
    print(f" {'上涨比':>7}")
    print('-' * 80)
    
    for cls in ['止损', '移动止盈', '到期']:
        items = by_type.get(cls, [])
        if not items: continue
        print(f"{cls:<10} {len(items):>4}", end='')
        for m in range(6):
            vals = [r['fwd'][m] for r in items if r['fwd'][m] is not None]
            avg = sum(vals) / len(vals) if vals else 0
            print(f" {avg:>+7.1f}%", end='')
        # 上涨比
        all_fwd = [r['fwd'][m] for r in items for m in range(6) if r['fwd'][m] is not None]
        pos = sum(1 for v in all_fwd if v > 0)
        print(f" {pos/len(all_fwd)*100:>6.0f}%")
    
    # 全局统计
    early_exits = [r for r in results if max(r['fwd']) > r['profit_pct'] + 10]
    print(f"\n卖早了(后续>卖出价+10%): {len(early_exits)}/{len(results)} = {len(early_exits)/len(results)*100:.0f}%")
    
    # 最惨的"割在地板上"
    worst = sorted(results, key=lambda r: max(r['fwd']) - r['profit_pct'], reverse=True)[:5]
    print("\n最大的'割在地板上':")
    for r in worst:
        print(f"  {r['name']} {r['buy_date']}→{r['sell_date']} 卖出时{r['profit_pct']:+.1f}%, "
              f"6月后最高可达{max(r['fwd']):+.1f}% (差{max(r['fwd'])-r['profit_pct']:.0f}pp)")
    
    return results
```

**关键判断**：
- 止损组 1 月后中位数 > 0 → **止损太紧，被震荡洗出**
- 止损组 3 月后中位数 > +10% → **止损是策略最大漏洞**
- 移动止盈组后续中位数 < 0 → 止盈点合理
- 到期组后续中位数 > 0 → 考虑延长持有期

### 2.5 止损幅度分析（如果有动态止损）

```python
def analyze_stop_distribution(data):
    """分析每笔交易的止损参数分布"""
    trades = [t for t in data['trades'] if t.get('sell_reason','') != '期末持仓']
    
    stops = []
    for t in trades:
        br = t.get('buy_reason', '')
        # 尝试从 buy_reason 提取止损参数
        # 格式可能是 "ATR3.2%止损-12%" 或直接看实际亏损
        if 'ATR' in br and '止损' in br:
            try:
                # 提取 ATR 值和止损百分比
                import re
                atr_match = re.search(r'ATR([\d.]+)%', br)
                stop_match = re.search(r'止损(-?[\d.]+)%', br)
                if atr_match and stop_match:
                    stops.append({
                        'atr': float(atr_match.group(1)),
                        'stop': float(stop_match.group(1)),
                        'name': t['name'],
                        'actual': t['profit_pct'],
                    })
            except:
                pass
    
    if stops:
        # 按止损幅度分桶
        from collections import Counter
        buckets = Counter()
        for s in stops:
            b = int(abs(s['stop']) // 2) * 2
            buckets[f'-{b}~-{b+2}%'] += 1
        
        print("\n止损幅度分布:")
        for k in sorted(buckets.keys(), key=lambda x: int(x.split('~')[0][1:].split('%')[0])):
            bar = '█' * buckets[k]
            print(f"  {k}: {bar} {buckets[k]}笔")
        
        avg_stop = sum(abs(s['stop']) for s in stops) / len(stops)
        print(f"  平均止损: {avg_stop:.1f}%")
```

---

## 第 3 步：根因诊断

基于第 2 步的数据，判断策略的核心问题：

| 症状 | 根因 | 方向 |
|------|------|------|
| 交易数=0 | gate 或信号逻辑有 bug | 检查 market_gate、generate_signals 调用时序 |
| 止损组后续大涨 | 止损太紧/太傻（一刀切） | ATR 动态止损、放宽幅度 |
| 胜率太低(<30%) | 买入时机不对 | 加确认条件（如广度回升） |
| 盈亏比<1.0 | 亏损比盈利大 | 收紧止损或放宽止盈 |
| 牛熊年分化极大 | 策略过度依赖牛市 | 加熊市过滤（如大盘MA60下方不买） |
| Top 5 > 60% 利润 | 依赖小样本 | 增加交易频率，降低单笔集中度 |
| 某年突然亏损大 | 参数过拟合 | 检查该年市场特征 |

---

## 第 4 步：修复与重跑

### 修改策略代码

基于诊断结果，精准修改策略参数或逻辑。常见的修改模式：

```python
# 模式 1: 翻转选股逻辑
# 原来: close > ma60 (买强势) → 改为 close < ma60 * 0.92 (买超跌)

# 模式 2: 固定止损 → ATR 动态止损
# 原来: STOP_LOSS_PCT = -12
# 改为: stop_pct = -max(8, min(22, 2.5 * atr_pct * 100))

# 模式 3: 去掉矛盾逻辑
# 原来: 恐慌买 + 过热卖 (矛盾!)
# 改为: 只保留恐慌买，到期/止盈卖

# 模式 4: 加确认条件
# 原来: 广度 < 25% 就买
# 改为: 广度 < 25% 且广度 > 3天前的广度 (恐慌见底)
```

### 重跑回测

```bash
cd d:/my_import/sync_content/code/b_quant
python -m backtest.strategy.strategy_{策略名}
```

### 对比结果

```python
# 加载新旧两个 archive，对比关键指标
def compare_results(old_path, new_path):
    old = json.load(open(old_path, 'r', encoding='utf-8'))
    new = json.load(open(new_path, 'r', encoding='utf-8'))
    
    os = old['summary']
    ns = new['summary']
    
    metrics = [
        ('总收益%', 'total_return_pct'),
        ('最大回撤%', 'max_drawdown_pct'),
        ('胜率%', 'win_rate_pct'),
        ('交易笔数', 'trade_count'),
        ('平均盈利%', 'avg_profit_pct'),
        ('盈亏比', 'profit_factor'),
    ]
    
    print(f"{'指标':<12} {'旧':>8} {'新':>8} {'变化':>8}")
    print('-' * 40)
    for label, key in metrics:
        ov = os[key]; nv = ns[key]
        if isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
            chg = nv - ov
            print(f"{label:<12} {ov:>8.2f} {nv:>8.2f} {chg:>+8.2f}")
```

---

## 第 5 步：导出 CSV（给用户做 Excel 深挖）

```python
import csv

def export_trades_csv(data, output_path):
    """导出交易明细 + 年汇总"""
    trades = data['trades']
    
    with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        
        # Sheet 1: 年度汇总
        w.writerow(['=== 年度汇总 ==='])
        w.writerow(['年份', '笔数', '已平仓', '胜率%', '累计盈亏', '累计%', '平均%', '最佳%', '最差%'])
        
        by_year = defaultdict(list)
        for t in trades:
            by_year[t['buy_date'][:4]].append(t)
        
        for year in sorted(by_year):
            items = by_year[year]
            closed = [t for t in items if t.get('sell_reason') != '期末持仓']
            wins = sum(1 for t in closed if t['profit'] > 0)
            tp = sum(t['profit'] for t in closed)
            tpp = sum(t['profit_pct'] for t in closed)
            w.writerow([
                year, len(items), len(closed),
                f"{wins/len(closed)*100:.0f}" if closed else '-',
                f"{tp:,.0f}", f"{tpp:.1f}%",
                f"{tpp/len(closed):.1f}%" if closed else '-',
                f"{max(t['profit_pct'] for t in closed):.1f}%" if closed else '-',
                f"{min(t['profit_pct'] for t in closed):.1f}%" if closed else '-',
            ])
        
        # Sheet 2: 交易明细
        w.writerow([])
        w.writerow(['=== 交易明细 ==='])
        w.writerow(['买入日', '卖出日', '股票', '代码', '买入价', '卖出价', '盈亏%', '盈亏额', '持天', '买入原因', '卖出原因'])
        
        for t in sorted(trades, key=lambda x: x['buy_date']):
            hold = ''
            try:
                bd = datetime.strptime(t['buy_date'], '%Y-%m-%d')
                sd = datetime.strptime(t['sell_date'], '%Y-%m-%d')
                hold = (sd - bd).days
            except: pass
            
            w.writerow([
                t['buy_date'], t['sell_date'], t['name'], t['code'],
                t['buy_price'], t['sell_price'],
                f"{t['profit_pct']:.1f}%", f"{t['profit']:,.0f}",
                hold, t.get('buy_reason',''), t.get('sell_reason',''),
            ])
    
    print(f"导出: {output_path}")
```

---

## 实战示例：market_bottom 策略的完整循环

```
Step 1 概览: 0笔交易, 0%收益 → 策略根本没触发
Step 2 诊断: _CURRENT_GATE 时序 bug, market_gate 在 generate_signals 之后才调用
Step 3 根因: gate 时序 + 选股逻辑反了(买强势而非超跌) + 市场过热卖出矛盾
Step 4 修复: 
  v1: 修 gate → 421笔, -44.65% (活了但亏)
  v2: 翻转为买超跌 → 57笔, +0.70% (扭亏)
  v3: 收紧选股+放宽止损 → 59笔, +80.26% (起飞)
  v4: ATR 动态止损 → 56笔, +85.12%, PF=2.17 (优化)
Step 5 导出: CSV 含逐笔交易 + 卖出后追踪
```

---

## 不允许做的事

- **不跑多策略对比**（那是 battle 接口的事）
- **不修改数据库**
- **不在没诊断清楚前盲目改参数** — 先看数据，再动手
