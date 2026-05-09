import sys
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup
import re

url = "http://q.10jqka.com.cn/gn/detail/code/301558/"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}
r = requests.get(url, headers=headers, timeout=15)
r.encoding = "gbk"

# Find all script src refs
scripts = re.findall(r'<script[^>]*src=["\']([^"\']+)["\']', r.text)
print("Script src:")
for s in scripts:
    print(f"  {s}")

# Search for mpager or pagination related JS
for pattern in [r'mpager', r'changePage', r'pager', r'\.init\(\)', r'ajax', r'get\s*\(', r'post\s*\(']:
    matches = re.findall(pattern, r.text, re.IGNORECASE)
    if matches:
        print(f"\n'{pattern}': {len(matches)} matches")

# Try the mobile page
mobile_url = "http://m.10jqka.com.cn/gn/detail/code/301558/"
r2 = requests.get(mobile_url, headers=headers, timeout=10)
print(f"\nmobile page: status={r2.status_code}, len={len(r2.text)}")

# Try API v2
api_url = "http://q.10jqka.com.cn/api/gn/detail/code/301558/page/1"
r3 = requests.get(api_url, headers=headers, timeout=10)
print(f"api v1: status={r3.status_code}, len={len(r3.text)}")
if r3.text.strip():
    print(f"  content[:300]: {r3.text[:300]}")
