#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全量下载 + 价增量增扫描 + QQ推送

一次性完成:
  1. 下载股票基础信息 (~4900只A股)
  2. 下载200天日K线数据
  3. 价增量增策略扫描
  4. 评分排序
  5. QQ推送最佳3只

用法:
  python script/full_download_and_scan.py               # 完整流程
  python script/full_download_and_scan.py --days 100    # 只下载100天
  python script/full_download_and_scan.py --skip-download  # 跳过下载，直接扫描
  python script/full_download_and_scan.py --push-top 3  # 推送前3只
"""
import sys
import json
import os
import time
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import baostock as bs
from sqlalchemy import create_engine, func, desc
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL, K_FIELDS, K_FREQUENCY
from models.stock import StockInfo, StockDaily
from backtest.indicators import sma
from backtest.portfolio import _build_market_stats
from backtest.strategy.strategy_price_volume_rising import (
    _compute_price_volume_dynamics,
    market_gate,
    PRICE_UP_5D_MIN,
    VOL_RATIO_BUY,
    VOL_RATIO_MAX,
    VOL_TREND_ACCEL,
    UP_VOL_RATIO,
    PRICE_ABOVE_MA10_MAX,
    MIN_CONSEC_UP,
)

# ============ 参数 ============
DOWNLOAD_DAYS = 200           # 日K线下载天数
PUSH_TOP = 3                  # 推送前N只
BATCH_COMMIT = 100            # 每N只股票 commit 一次


# ============ Phase 1: 下载股票基础信息 ============

def download_stock_basic(session) -> int:
    """下载全量A股基础信息"""
    print("[1/4] 下载股票基础信息...")
    bs.login()
    try:
        rs = bs.query_stock_basic()
        if rs.error_code != "0":
            print(f"  query_stock_basic 失败: {rs.error_msg}")
            return 0

        count = 0
        while rs.next():
            row = rs.get_row_data()
            code, name, ipo_date, out_date, sec_type, status = row[0], row[1], row[2], row[3], row[4], row[5]

            # 只保留正常A股
            if sec_type != "1" or status != "1":
                continue
            if "ST" in name.upper():
                continue

            existing = session.query(StockInfo).filter(StockInfo.code == code).first()
            if existing:
                continue

            session.add(StockInfo(
                code=code,
                name=name,
                market="sh" if code.startswith("sh") else "sz",
                ipo_date=ipo_date if ipo_date else None,
                type=sec_type,
                status=1,
            ))
            count += 1

            if count % 500 == 0:
                session.commit()
                print(f"  已入库 {count} 只...")

        session.commit()
        print(f"  完成: 新增 {count} 只股票")
        return count
    finally:
        bs.logout()


# ============ Phase 2: 下载日K线 ============

def download_daily_k(session, days: int):
    """下载全量日K线数据"""
    print(f"[2/4] 下载日K线数据 (最近{days}天)...")

    # 获取所有股票代码
    codes = [r[0] for r in session.query(StockInfo.code)
             .filter(StockInfo.type == "1", StockInfo.status == 1).all()]
    print(f"  股票总数: {len(codes)}")

    if not codes:
        print("  错误: 无股票基础数据，请先下载")
        return

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days + 10)).strftime("%Y-%m-%d")

    bs.login()
    count = 0
    total = len(codes)
    start_time = time.time()
    consecutive_failures = 0

    for idx, code in enumerate(codes, 1):
        try:
            rs = bs.query_history_k_data_plus(
                code, K_FIELDS,
                start_date=start_date, end_date=end_date,
                frequency=K_FREQUENCY, adjustflag="3"
            )
            if rs.error_code != "0":
                consecutive_failures += 1
                if consecutive_failures >= 5:
                    print(f"\n  重连 BaoStock...")
                    bs.logout()
                    bs.login()
                    consecutive_failures = 0
                continue

            # 使用 get_data() 而非 next() 迭代器（后者会卡死）
            data = rs.get_data()
            if data.empty:
                continue
            rows_data = data.values.tolist()

            consecutive_failures = 0

            for row in rows_data:
                trade_date = str(row[0])[:10]
                existing = session.query(StockDaily).filter_by(
                    code=code, trade_date=trade_date
                ).first()
                # 安全转换：处理 DataFrame 中可能的 nan/None/空字符串
                def _safe_float(v, default=None):
                    try:
                        if v is None or (isinstance(v, float) and v != v):  # NaN check
                            return default
                        return float(v)
                    except (ValueError, TypeError):
                        return default

                def _safe_int(v, default=None):
                    try:
                        if v is None or (isinstance(v, float) and v != v):
                            return default
                        return int(float(v))
                    except (ValueError, TypeError):
                        return default

                vals = {
                    "open": _safe_float(row[1]),
                    "high": _safe_float(row[2]),
                    "low": _safe_float(row[3]),
                    "close": _safe_float(row[4]),
                    "volume": _safe_int(row[5]),
                    "amount": _safe_float(row[6]),
                    "turn": _safe_float(row[7]),
                    "pe_ttm": _safe_float(row[8]),
                }
                if existing:
                    for k, v in vals.items():
                        setattr(existing, k, v)
                else:
                    session.add(StockDaily(code=code, trade_date=trade_date, **vals))
                count += 1

            if idx % BATCH_COMMIT == 0:
                session.commit()

        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 5:
                bs.logout()
                bs.login()
                consecutive_failures = 0
            continue

        # 进度日志
        if idx % 200 == 0 or idx == total:
            elapsed = time.time() - start_time
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (total - idx) / rate / 60 if rate > 0 else 0
            print(f"  进度: {idx}/{total} ({idx/total*100:.0f}%) | "
                  f"速度 {rate:.1f}只/秒 | 预计剩余 {eta:.0f}分钟 | "
                  f"已入库 {count} 行")

    session.commit()
    bs.logout()
    elapsed = time.time() - start_time
    print(f"  完成: {count} 行K线 | 耗时 {elapsed/60:.1f}分钟")


# ============ Phase 3: 扫描候选股 ============

def _check_buy_conditions(closes, volumes, i, ma10, vol_ma5, vol_ma20):
    """检查价增量增买入条件"""
    if ma10[i] is None or vol_ma5[i] is None or vol_ma20[i] is None:
        return None
    if vol_ma20[i] == 0:
        return None

    close = closes[i]
    vol_ratio = vol_ma5[i] / vol_ma20[i]

    # 5日涨幅
    if i < 5 or closes[i - 5] <= 0:
        return None
    chg_5d = (close - closes[i - 5]) / closes[i - 5]
    if chg_5d < PRICE_UP_5D_MIN:
        return None

    # 站上MA10
    if close <= ma10[i]:
        return None

    # 量能放大
    if vol_ratio < VOL_RATIO_BUY or vol_ratio > VOL_RATIO_MAX:
        return None

    # 量能加速
    if i >= 6:
        recent_3 = sum(volumes[i - 2:i + 1]) / 3
        prior_3 = sum(volumes[i - 5:i - 2]) / 3
        if prior_3 > 0 and recent_3 / prior_3 < VOL_TREND_ACCEL:
            return None

    # 量价配合
    dyn = _compute_price_volume_dynamics(closes, volumes, i)
    if dyn["up_vol_ratio"] < UP_VOL_RATIO:
        return None

    # 不追高
    if close > ma10[i] * PRICE_ABOVE_MA10_MAX:
        return None

    # 连续上涨
    if dyn["consecutive_up"] < MIN_CONSEC_UP:
        return None

    chg_10d = 0
    if i >= 10 and closes[i - 10] > 0:
        chg_10d = (close - closes[i - 10]) / closes[i - 10]
    chg_20d = 0
    if i >= 20 and closes[i - 20] > 0:
        chg_20d = (close - closes[i - 20]) / closes[i - 20]

    dist_from_ma10 = (close - ma10[i]) / ma10[i]

    return {
        "code": "", "name": "", "market": "",
        "chg_5d": round(chg_5d, 5),
        "chg_10d": round(chg_10d, 5),
        "chg_20d": round(chg_20d, 5),
        "close": close,
        "ma10": round(ma10[i], 2),
        "dist_from_ma10": round(dist_from_ma10, 4),
        "vol_ratio": round(vol_ratio, 2),
        "vol_ma5": round(vol_ma5[i], 0),
        "vol_ma20": round(vol_ma20[i], 0),
        "up_vol_ratio": dyn["up_vol_ratio"],
        "up_days": dyn["up_days"],
        "down_days": dyn["down_days"],
        "consecutive_up": dyn["consecutive_up"],
        "daily_chg": round((closes[i] - closes[i - 1]) / closes[i - 1], 5) if i > 0 and closes[i - 1] > 0 else 0,
    }


def _clamp(val, lo, hi):
    return max(lo, min(hi, val))


def _score_candidates(candidates):
    """4维度评分"""
    for c in candidates:
        # 价格趋势强度
        chg_5d_pct = c["chg_5d"] * 100
        score_price = _clamp((chg_5d_pct - PRICE_UP_5D_MIN * 100) / (10 - PRICE_UP_5D_MIN * 100) * 80, 0, 80)
        if c["chg_10d"] > 0.02:
            score_price += 10
        if c["chg_20d"] > 0:
            score_price += 10
        c["score_price"] = round(_clamp(score_price, 0, 100), 1)

        # 量能强度
        vr = c["vol_ratio"]
        if vr <= 3.5:
            score_vol = (vr - VOL_RATIO_BUY) / (3.5 - VOL_RATIO_BUY) * 100
        else:
            score_vol = 100 - (vr - 3.5) / 0.5 * 40
        c["score_volume"] = round(_clamp(score_vol, 0, 100), 1)

        # 量价配合
        uvr = c["up_vol_ratio"]
        score_coord = (uvr - UP_VOL_RATIO) / (2.5 - UP_VOL_RATIO) * 100
        if c["up_days"] >= 7:
            score_coord += 10
        c["score_coordination"] = round(_clamp(score_coord, 0, 100), 1)

        # 趋势质量
        cu = c["consecutive_up"]
        cons_score = _clamp((cu - MIN_CONSEC_UP) / (5 - MIN_CONSEC_UP) * 60, 0, 60)
        dist = c["dist_from_ma10"] * 100
        if 1.0 <= dist <= 3.0:
            pos_score = 40
        elif 0 < dist < 1.0:
            pos_score = dist / 1.0 * 30
        elif 3.0 < dist <= 6.0:
            pos_score = (6.0 - dist) / 3.0 * 30
        else:
            pos_score = 0
        c["score_quality"] = round(_clamp(cons_score + pos_score, 0, 100), 1)

        # 综合评分
        c["score"] = round(
            c["score_price"] * 0.30
            + c["score_volume"] * 0.30
            + c["score_coordination"] * 0.25
            + c["score_quality"] * 0.15,
            1,
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    for i, c in enumerate(candidates, 1):
        c["rank"] = i
    return candidates


def scan_candidates(session):
    """扫描全市场价增量增候选股"""
    print("[3/4] 扫描价增量增候选股...")

    latest = session.query(func.max(StockDaily.trade_date)).scalar()
    print(f"  数据库最新日期: {latest}")

    # 活跃股票
    latest_rows = (
        session.query(StockInfo, StockDaily)
        .join(StockDaily, StockInfo.code == StockDaily.code)
        .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date == latest)
        .all()
    )
    stock_map = {
        stock.code: {"code": stock.code, "name": stock.name, "market": stock.market}
        for stock, _ in latest_rows
    }
    print(f"  活跃股票: {len(stock_map)} 只")

    # 加载K线数据
    date_rows = (
        session.query(StockDaily.trade_date)
        .distinct()
        .order_by(desc(StockDaily.trade_date))
        .limit(200)
        .all()
    )
    if not date_rows:
        print("  错误: 无K线数据")
        return [], latest, {}
    cutoff = min(row[0] for row in date_rows)

    rows = (
        session.query(StockDaily)
        .join(StockInfo, StockDaily.code == StockInfo.code)
        .filter(StockInfo.type == "1", StockInfo.status == 1, StockDaily.trade_date >= cutoff)
        .order_by(StockDaily.code, StockDaily.trade_date)
        .all()
    )

    bars_by_code = defaultdict(list)
    for row in rows:
        bars_by_code[row.code].append({
            "trade_date": row.trade_date,
            "open": row.open, "high": row.high, "low": row.low, "close": row.close,
            "volume": row.volume, "amount": row.amount,
        })
    print(f"  加载 {len(bars_by_code)} 只股票的K线")

    # 扫描
    candidates = []
    scanned = 0
    skipped = 0

    for code, bars in bars_by_code.items():
        if len(bars) < 25:
            skipped += 1
            continue
        scanned += 1

        if bars[-1]["trade_date"] != latest:
            continue

        closes = [b["close"] for b in bars]
        volumes = [b.get("volume") or 0 for b in bars]
        ma10 = sma(closes, 10)
        vol_ma5 = sma(volumes, 5)
        vol_ma20 = sma(volumes, 20)
        idx = len(closes) - 1

        result = _check_buy_conditions(closes, volumes, idx, ma10, vol_ma5, vol_ma20)
        if result is None:
            continue

        stock = stock_map.get(code, {"code": code, "name": code, "market": ""})
        result["code"] = code
        result["name"] = stock.get("name", code)
        result["market"] = stock.get("market", "")
        candidates.append(result)

    print(f"  扫描: {scanned} 只 | 跳过(<25天): {skipped} 只 | 候选: {len(candidates)} 只")

    # 评分排序
    ranked = _score_candidates(candidates)
    if ranked:
        print(f"  最高评分: {ranked[0]['score']:.0f} (共{len(ranked)}只)")

    # 大盘统计
    market_stats = _build_market_stats(bars_by_code)

    return ranked, latest, market_stats


# ============ Phase 4: QQ推送 ============

def _fmt_pct(val):
    return f"{val * 100:+.1f}%"


def _weekday_str(date_str):
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{date_str} ({weekdays[dt.weekday()]})"
    except Exception:
        return date_str


def push_to_qq(ranked, market_stats, latest_date, top_n):
    """推送最佳N只到QQ"""
    print(f"[4/4] QQ推送最佳 {top_n} 只...")

    gate = market_gate(latest_date, market_stats)
    gate_ok = gate["allowed"]
    gate_label = "[可买]" if gate_ok else "[观望]"

    lines = [
        f"[*] 价增量增策略 — {_weekday_str(latest_date)}",
        f"大盘: {gate_label} | 候选: {len(ranked)}只",
        "",
    ]

    if not ranked:
        lines.append("无符合条件的候选股")
        lines.append(f"条件: 5日涨>{PRICE_UP_5D_MIN*100}% | 量比>{VOL_RATIO_BUY}x | 连涨≥{MIN_CONSEC_UP}天")
    else:
        show_n = min(len(ranked), top_n)
        for c in ranked[:show_n]:
            lines.append(
                f"{c['rank']}. {c['name']}({c['code']}) "
                f"评分{c['score']:.0f} "
                f"| 5日涨{_fmt_pct(c['chg_5d'])} "
                f"| 量比{c['vol_ratio']:.1f}x "
                f"| 连涨{c['consecutive_up']}天 "
                f"| 涨跌量比{c['up_vol_ratio']:.1f}x "
                f"| 距MA10 {c['dist_from_ma10']*100:+.1f}%"
            )

        if not gate_ok:
            lines.append("")
            lines.append(f"[!] 大盘不佳({'；'.join(gate['reasons'])})，仅供参考")

    lines.append("")
    lines.append("--- 价增量增策略 · 仅供参考 ---")

    msg = "\n".join(lines)
    print("\n" + msg)

    try:
        from models.qq_webhook import QQPusher
        pusher = QQPusher()
        if pusher.enabled:
            result = pusher.push_long_text(msg)
            print(f"\n  QQ推送完成: success={result['success']}, fail={result['fail']}")
        else:
            print("\n  QQ推送未启用，请检查 data/qq_config.json")
    except Exception as e:
        print(f"\n  QQ推送失败: {e}")


# ============ 主流程 ============

def main():
    import argparse
    parser = argparse.ArgumentParser(description="全量下载 + 价增量增扫描 + QQ推送")
    parser.add_argument("--days", type=int, default=DOWNLOAD_DAYS, help="日K线下载天数")
    parser.add_argument("--push-top", type=int, default=PUSH_TOP, help="推送前N只")
    parser.add_argument("--skip-download", action="store_true", help="跳过下载，直接扫描")
    parser.add_argument("--no-push", action="store_true", help="不推送到QQ")
    args = parser.parse_args()

    print("=" * 60)
    print("  价增量增策略 — 全量下载 + 扫描 + QQ推送")
    print("=" * 60)
    print(f"  参数: 下载{args.days}天K线 | 推送前{args.push_top}只 | "
          f"跳过下载={'是' if args.skip_download else '否'}")
    print("")

    engine = create_engine(DATABASE_URL, echo=False)
    Session = sessionmaker(bind=engine)
    sess = Session()

    try:
        # ---- Phase 1+2: 下载 ----
        if not args.skip_download:
            t0 = time.time()

            # 1. 基础信息
            total_stocks = sess.query(StockInfo.code).filter(
                StockInfo.type == "1", StockInfo.status == 1
            ).count()
            if total_stocks < 100:
                download_stock_basic(sess)
            else:
                print(f"[1/4] 股票基础信息已存在: {total_stocks} 只，跳过下载")

            # 2. 日K线
            download_daily_k(sess, args.days)

            t1 = time.time()
            print(f"\n  下载总耗时: {(t1 - t0) / 60:.1f}分钟")
        else:
            print("[1/4] 跳过下载")
            print("[2/4] 跳过下载")

        # ---- Phase 3: 扫描 ----
        ranked, latest_date, market_stats = scan_candidates(sess)

        # ---- Phase 4: 推送 ----
        if not args.no_push:
            push_to_qq(ranked, market_stats, latest_date, args.push_top)
        else:
            print("[4/4] 跳过QQ推送")

        # ---- 终端输出 ----
        if ranked:
            print("\n" + "=" * 60)
            print(f"  最佳 {min(len(ranked), args.push_top)} 只候选股:")
            print("=" * 60)
            for c in ranked[:args.push_top]:
                print(f"\n  [{c['rank']}] {c['code']} {c['name']} — 评分 {c['score']:.0f}")
                print(f"      价格: 5日{_fmt_pct(c['chg_5d'])} | 10日{_fmt_pct(c['chg_10d'])} | 20日{_fmt_pct(c['chg_20d'])}")
                print(f"      量能: 量比{c['vol_ratio']:.1f}x | 涨跌量比{c['up_vol_ratio']:.1f}x | 连涨{c['consecutive_up']}天")
                print(f"      位置: 距MA10({c['ma10']}) {c['dist_from_ma10']*100:+.1f}% | 今日{_fmt_pct(c['daily_chg'])}")
                print(f"      分项: 价格{c['score_price']:.0f} | 量能{c['score_volume']:.0f} | 配合{c['score_coordination']:.0f} | 质量{c['score_quality']:.0f}")
        else:
            print("\n  未发现符合条件的候选股")

    finally:
        sess.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
