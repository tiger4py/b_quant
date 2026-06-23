"""测试新浪实时行情"""
import urllib.request
import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

codes = ["sz002378", "sh600596", "sh600999", "sz002137", "sz300880"]
url = "http://hq.sinajs.cn/list=" + ",".join(codes)

req = urllib.request.Request(url)
req.add_header("Referer", "https://finance.sina.com.cn")
resp = urllib.request.urlopen(req, timeout=10)
data = resp.read().decode("gbk")

for line in data.strip().split("\n"):
    print(line)
    print()
