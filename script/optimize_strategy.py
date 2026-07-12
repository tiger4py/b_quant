# -*- coding: utf-8 -*-
"""
策略自动优化器 — 系统性地搜索参数改进方向

用法:
  python script/optimize_strategy.py --strategy market_bottom --iterations 10

流程:
  1. 记录基线参数和结果
  2. 按优先级逐轮尝试参数变化
  3. 每次改动 → 跑回测 → 跑分析Excel → 记录结果
  4. 保留最佳版本，输出优化轨迹 CSV
"""

import json
import sys
import os
import re
import shutil
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
from collections import OrderedDict

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

# ============ 优化计划定义 ============

# 格式: (round, name, param_changes_dict, description)
# param_changes 中 key 是策略文件中的常量名，value 是新值

OPT_PLAN = [
    # === P0: 止损继续放宽 ===
    (1,  'ATR上限40',   {'ATR_STOP_MAX_EXTREME': '40'},  '极度恐慌止损上限 35→40'),
    (2,  'ATR上限38',   {'ATR_STOP_MAX_EXTREME': '38'},  '极度恐慌止损上限 35→38'),
    (3,  '宽容期50天',  {'TIME_DECAY_GRACE_DAYS': '50'}, '时间衰减宽容期 40→50'),
    (4,  '宽容期60天',  {'TIME_DECAY_GRACE_DAYS': '60'}, '时间衰减宽容期 40→60'),

    # === P1: 止盈阶梯化 ===
    (5,  '止盈回落25',  {'TRAILING_STOP_PCT': '25'},     '移动止盈回落 22%→25%'),
    (6,  '止盈回落28',  {'TRAILING_STOP_PCT': '28'},     '移动止盈回落 22%→28%'),

    # === P2: 极度恐慌倍率 ===
    (7,  '恐慌倍率3.5', {'ATR_STOP_MULT_EXTREME': '3.5'}, '极度恐慌止损倍率 3.0→3.5'),
    (8,  '恐慌倍率3.3', {'ATR_STOP_MULT_EXTREME': '3.3'}, '极度恐慌止损倍率 3.0→3.3'),

    # === P3: 时间衰减 ===
    (9,  '衰减倍率1.8', {'TIME_DECAY_MULT': '1.8'},      '时间衰减倍率 1.5→1.8'),
    (10, '过渡期30天',  {'TIME_DECAY_TRANSITION': '30'},  '过渡期 20→30天'),
    (11, '过渡期40天',  {'TIME_DECAY_TRANSITION': '40'},  '过渡期 20→40天'),

    # === P4: 选股条件微调 ===
    (12, '超跌-10%',   {'MAX_BELOW_MA_PCT': '-0.10'},    '超跌条件 -8%→-10%（更严格）'),
    (13, '5日放宽-10%', {'MAX_DECLINE_5D': '-0.10'},     '5日跌幅上限 -8%→-10%（更宽）'),
]

# ============ 核心工具 ============

STRATEGY_FILE = None  # 运行时设置


def read_strategy_params(filepath):
    """读取策略文件中的参数常量"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    params = {}
    pattern = re.compile(r'^([A-Z_][A-Z_0-9]*)\s*=\s*([0-9.-]+)', re.MULTILINE)
    for m in pattern.finditer(content):
        params[m.group(1)] = m.group(2)
    return params, content


def apply_param_changes(content, changes):
    """在策略文件内容中替换参数值"""
    for name, new_val in changes.items():
        # 匹配形如 NAME = value 的参数定义
        pattern = re.compile(
            rf'(^{name}\s*=\s*)([0-9.-]+)',
            re.MULTILINE
        )
        content = pattern.sub(rf'\g<1>{new_val}', content)
    return content


def write_strategy(content):
    """写回策略文件"""
    with open(STRATEGY_FILE, 'w', encoding='utf-8') as f:
        f.write(content)


def run_backtest(strategy_id):
    """跑回测，返回 (success, json_path)"""
    cmd = [
        sys.executable,
        str(ROOT_DIR / 'script/run_backtest.py'),
        '--strategy', strategy_id,
        '--max-positions', '5',
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                          cwd=str(ROOT_DIR), encoding='utf-8', errors='replace')
        # 找 archive 路径
        for line in r.stdout.split('\n'):
            if '[archive]' in line:
                # 格式: [archive] strategy_id -> path
                path = line.split('->')[-1].strip()
                return True, path
        return False, r.stderr[-500:] if r.stderr else 'unknown'
    except subprocess.TimeoutExpired:
        return False, 'timeout'
    except Exception as e:
        return False, str(e)


def run_analysis(strategy_id):
    """跑分析生成 Excel，返回 (success, xlsx_path)"""
    cmd = [
        sys.executable,
        str(ROOT_DIR / 'script/analyze_trades.py'),
        '--strategy', strategy_id,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                          cwd=str(ROOT_DIR), encoding='utf-8', errors='replace')
        for line in r.stdout.split('\n'):
            if '分析报告已生成' in line:
                path = line.split(':')[-1].strip()
                return True, path
        return False, r.stderr[-300:] if r.stderr else 'no output'
    except Exception as e:
        return False, str(e)


def read_backtest_summary(json_path):
    """读取回测结果摘要"""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    s = data['summary']
    return {
        'return': s['total_return_pct'],
        'drawdown': s['max_drawdown_pct'],
        'win_rate': s['win_rate_pct'],
        'trades': s['trade_count'],
        'avg_profit': s['avg_profit_pct'],
        'pf': s.get('profit_factor') or 0,
        'final_equity': s['final_equity'],
    }


# ============ 主流程 ============

def optimize(strategy_id, max_iterations=10):
    """主优化循环"""
    global STRATEGY_FILE
    STRATEGY_FILE = ROOT_DIR / 'backtest/strategy' / f'strategy_{strategy_id}.py'

    if not STRATEGY_FILE.exists():
        print(f"策略文件不存在: {STRATEGY_FILE}")
        return

    # 备份原始文件
    backup = STRATEGY_FILE.read_text(encoding='utf-8')
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_dir = ROOT_DIR / 'data/strategy' / strategy_id / 'optimize_logs'
    log_dir.mkdir(parents=True, exist_ok=True)

    # 优化轨迹
    trajectory = []
    best_result = None
    best_params = None
    best_round = 0

    print(f"{'='*80}")
    print(f"策略优化器 — {strategy_id}")
    print(f"最大迭代: {max_iterations}  日志目录: {log_dir}")
    print(f"{'='*80}")

    # ---- Round 0: 基线 ----
    print(f"\n[Round 0] 基线测试...")
    params, content = read_strategy_params(STRATEGY_FILE)

    ok, json_path = run_backtest(strategy_id)
    if not ok:
        print(f"  基线回测失败: {json_path}")
        STRATEGY_FILE.write_text(backup, encoding='utf-8')
        return

    baseline = read_backtest_summary(json_path)
    best_result = baseline
    best_params = {k: v for k, v in params.items() if k not in ('ATR_PERIOD', 'MA_TREND_PERIOD', 'MAX_HOLD_DAYS')}

    print(f"  基线: 收益{baseline['return']:+.1f}% 回撤{baseline['drawdown']:.1f}% "
          f"胜率{baseline['win_rate']:.1f}% PF={baseline['pf']:.2f} 交易{baseline['trades']}笔")

    # 保存基线 Excel
    ok, xlsx = run_analysis(strategy_id)
    trajectory.append({
        'round': 0, 'name': '基线', 'status': 'baseline',
        'return': baseline['return'], 'drawdown': baseline['drawdown'],
        'win_rate': baseline['win_rate'], 'trades': baseline['trades'],
        'avg_profit': baseline['avg_profit'], 'pf': baseline['pf'],
        'json': json_path, 'xlsx': xlsx if ok else '',
        'params': str(best_params),
    })

    # ---- 迭代优化 ----
    for round_num, name, changes, desc in OPT_PLAN:
        if round_num > max_iterations:
            break

        print(f"\n[Round {round_num}] {name} — {desc}")

        # 应用参数变更
        params, content = read_strategy_params(STRATEGY_FILE)
        # 验证所有变更参数存在
        missing = [k for k in changes if k not in params]
        if missing:
            print(f"  SKIP: 参数不存在 {missing}")
            continue

        modified = apply_param_changes(content, changes)
        write_strategy(modified)

        # 跑回测
        ok, json_path = run_backtest(strategy_id)
        if not ok:
            print(f"  回测失败: {json_path}")
            STRATEGY_FILE.write_text(backup, encoding='utf-8')
            continue

        result = read_backtest_summary(json_path)
        ok2, xlsx = run_analysis(strategy_id)

        # 对比
        chg_return = result['return'] - baseline['return']
        chg_pf = result['pf'] - baseline['pf']
        chg_dd = result['drawdown'] - baseline['drawdown']

        improved = result['return'] > best_result['return'] * 1.005  # 收益提升 > 0.5%

        status = 'BEST' if improved else 'worse'
        if improved:
            best_result = result
            best_round = round_num
            # 更新策略文件为当前版本
            backup = modified
        else:
            # 回退
            write_strategy(backup)

        print(f"  {status:6s} 收益{result['return']:+.1f}% ({chg_return:+.1f}pp) "
              f"PF={result['pf']:.2f} ({chg_pf:+.2f}) "
              f"回撤{result['drawdown']:.1f}% ({chg_dd:+.1f}pp) "
              f"胜率{result['win_rate']:.1f}% 交易{result['trades']}笔")

        trajectory.append({
            'round': round_num, 'name': name, 'status': status,
            'return': result['return'], 'drawdown': result['drawdown'],
            'win_rate': result['win_rate'], 'trades': result['trades'],
            'avg_profit': result['avg_profit'], 'pf': result['pf'],
            'json': json_path, 'xlsx': xlsx if ok2 else '',
            'params': str(changes),
        })

    # ---- 最终：恢复到最佳版本 ----
    print(f"\n{'='*80}")
    print(f"优化完成。最佳: Round {best_round}")
    best_t = [t for t in trajectory if t['status'] == 'BEST']
    if best_t:
        # 最终结果
        print(f"  收益: {best_result['return']:+.2f}%  回撤: {best_result['drawdown']:.2f}%")
        print(f"  胜率: {best_result['win_rate']:.1f}%  PF: {best_result['pf']:.2f}")
        print(f"  交易: {best_result['trades']}笔  平均盈亏: {best_result['avg_profit']:.2f}%")

    # ---- 导出优化轨迹 CSV ----
    csv_path = log_dir / f'optimize_trajectory_{timestamp}.csv'
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        import csv
        w = csv.writer(f)
        w.writerow(['轮次', '名称', '状态', '收益%', '回撤%', '胜率%', 'PF',
                    '交易笔数', '平均盈亏%', 'JSON', 'Excel', '参数变更'])
        for t in trajectory:
            w.writerow([t['round'], t['name'], t['status'],
                       f"{t['return']:.2f}", f"{t['drawdown']:.2f}",
                       f"{t['win_rate']:.1f}", f"{t['pf']:.2f}",
                       t['trades'], f"{t['avg_profit']:.2f}",
                       t['json'], t['xlsx'], t['params']])

    print(f"\n优化轨迹: {csv_path}")

    # 确保最佳版本是最终文件
    if best_round > 0:
        write_strategy(backup)
        print(f"策略文件已保留最佳版本 (Round {best_round})")

    return trajectory


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='策略自动优化器')
    parser.add_argument('--strategy', type=str, default='market_bottom')
    parser.add_argument('--iterations', type=int, default=10)
    args = parser.parse_args()

    optimize(args.strategy, args.iterations)
