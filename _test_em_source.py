"""对比测试：东方财富源 vs 新浪源 下载 7月1号ETF数据"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import akshare as ak

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(ROOT_DIR, "data", "etf_codes.json"), "r", encoding="utf-8") as f:
    etf_list = json.load(f)

# 只测前100只，对比
test_list = etf_list[:100]
print(f"测试 {len(test_list)} 只ETF，东方财富源...\n")

success = 0
fail = 0
no_data = 0
fail_codes = []

for idx, etf in enumerate(test_list):
    code = etf["code"]
    try:
        # 只拉最近5天，减小请求体积
        df = ak.fund_etf_hist_em(symbol=code, period='daily', start_date='20260701', end_date='20260702', adjust='qfq')
        if df is None or len(df) == 0:
            no_data += 1
            if no_data <= 3:
                print(f"  [EMPTY] {code} {etf['name']}")
        else:
            success += 1
            if success <= 3:
                row = df.iloc[0]
                print(f"  [OK] {code} {etf['name']}: {row.get('日期','?')} open={row.get('开盘', '?')} close={row.get('收盘', '?')} vol={row.get('成交量', '?')}")
    except Exception as e:
        fail += 1
        err_msg = str(e)[:80]
        fail_codes.append((code, err_msg))
        if fail <= 5:
            print(f"  [FAIL] {code} {etf['name']}: {err_msg}")

    if (idx + 1) % 20 == 0:
        print(f"  进度: {idx+1}/{len(test_list)} | 成功:{success} 失败:{fail} 无数据:{no_data}")

    time.sleep(0.1)

print()
print("=" * 60)
print(f"东方财富源 — {len(test_list)}只测试结果:")
print(f"  成功:   {success} ({success/len(test_list)*100:.0f}%)")
print(f"  失败:   {fail} ({fail/len(test_list)*100:.0f}%)")
print(f"  无数据: {no_data} ({no_data/len(test_list)*100:.0f}%)")
print()
print(f"对比新浪源(前100只): 成功100, 失败0")
print(f"东方财富源:          成功{success}, 失败{fail}")

if fail_codes:
    print(f"\n失败详情:")
    for c, e in fail_codes[:10]:
        print(f"  {c}: {e}")
