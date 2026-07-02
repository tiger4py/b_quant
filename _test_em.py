from urllib import request
import json

# 东方财富 ETF 列表
url = 'https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&po=1&np=1&fltt=2&invt=2&fid=f3&fs=b:MK0204&fields=f12,f14'
r = request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
resp = request.urlopen(r, timeout=10)
data = json.loads(resp.read().decode('utf-8'))
total = data['data']['total']
print(f'ETF总数: {total}')
for item in data['data']['diff'][:5]:
    print(f'  {item["f12"]} {item["f14"]}')

# 测试单个 ETF 日线
print('\n测试 ETF 日线:')
url2 = 'https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.510050&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&end=20260702&lmt=3'
r2 = request.Request(url2, headers={'User-Agent': 'Mozilla/5.0'})
resp2 = request.urlopen(r2, timeout=10)
d2 = json.loads(resp2.read().decode('utf-8'))
if d2['data'] and d2['data']['klines']:
    for line in d2['data']['klines'][-3:]:
        print(f'  {line}')
else:
    print('  无数据')
