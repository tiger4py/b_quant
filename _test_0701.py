"""快速测试：只下载 2026-07-01 的ETF数据，统计覆盖条数"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from urllib import request

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET_DATE = "2026-07-01"

# 读ETF列表
with open(os.path.join(ROOT_DIR, "data", "etf_codes.json"), "r", encoding="utf-8") as f:
    etf_list = json.load(f)
print(f"ETF列表总数: {len(etf_list)}")

# 只拉7月1号
has_data = 0
no_data = 0
no_kline = 0
fail = 0

for idx, etf in enumerate(etf_list):
    code, name = etf["code"], etf["name"]
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=5000")
    req = request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = request.urlopen(req, timeout=15)
        raw = resp.read().decode("utf-8")
        klines = json.loads(raw) if raw and raw != "null" else []
    except Exception as e:
        fail += 1
        if fail <= 3:
            print(f"  [FAIL] {code} {name}: {e}")
        continue

    if not klines:
        no_kline += 1
        continue

    # 找7月1号
    found = [k for k in klines if k["day"] == TARGET_DATE]
    if found:
        has_data += 1
        if has_data <= 5:
            k = found[0]
            print(f"  [OK] {code} {name}: open={k['open']} close={k['close']} vol={k['volume']}")
    else:
        no_data += 1
        # 看看最后一条数据的日期
        last_date = klines[-1]["day"] if klines else "N/A"
        if no_data <= 5:
            print(f"  [MISS] {code} {name}: 最新日期={last_date}")

    if (idx + 1) % 100 == 0:
        print(f"  进度: {idx+1}/{len(etf_list)} | 有数据:{has_data} 无0701:{no_data} 无K线:{no_kline} 失败:{fail}")

    time.sleep(0.15)  # 控制频率

print()
print("=" * 60)
print(f"结果汇总:")
print(f"  ETF总数:     {len(etf_list)}")
print(f"  有7月1号数据: {has_data} ({has_data/len(etf_list)*100:.1f}%)")
print(f"  无7月1号数据: {no_data} ({no_data/len(etf_list)*100:.1f}%)")
print(f"  无任何K线:   {no_kline} ({no_kline/len(etf_list)*100:.1f}%)")
print(f"  请求失败:    {fail} ({fail/len(etf_list)*100:.1f}%)")
print(f"  有效覆盖率:  {has_data}/{has_data+no_data} = {has_data/(has_data+no_data)*100:.1f}% (排除无K线和失败)")
