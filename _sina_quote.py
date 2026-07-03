import urllib.request
url = "https://hq.sinajs.cn/list=sz300285"
req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
resp = urllib.request.urlopen(req, timeout=10)
raw = resp.read().decode("gbk")
print(raw)

# 解析
# var hq_str_sz300285="国瓷材料,92.79,93.50,91.50,98.82,..."
parts = raw.split('"')[1].split(",")
if len(parts) >= 9:
    name = parts[0]
    open_p = float(parts[1])
    yest_close = float(parts[2])
    price = float(parts[3])
    high = float(parts[4])
    low = float(parts[5])
    vol = int(parts[8])  # 成交量(手)
    print(f"\n--- 解析 ---")
    print(f"  名称: {name}")
    print(f"  昨收: {yest_close}  今开: {open_p}")
    print(f"  最新: {price}  最高: {high}  最低: {low}")
    print(f"  涨幅: {(price/yest_close-1)*100:+.2f}%")
    print(f"  成交量: {vol}手 = {vol/10000:.0f}万手")
else:
    print("解析失败")
