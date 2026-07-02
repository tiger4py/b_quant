"""从 etf.group 抓取所有 ETF 代码"""
from urllib import request
import re, json, os

url = 'http://www.etf.group/data/list1.html'
try:
    req = request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    resp = request.urlopen(req, timeout=15)
    html = resp.read().decode('utf-8')
    print(f'页面大小: {len(html)} 字符')

    # 尝试匹配常见格式: 代码(6位数字) + 名称
    # 格式1: 表格中的代码+名称
    pattern1 = re.findall(r'(\d{6})\s*[<>\s]+(\S+)', html)
    # 格式2: JSON 数据
    pattern2 = re.findall(r'"code"\s*:\s*"(\d{6})"[^}]*"name"\s*:\s*"([^"]+)"', html)
    # 格式3: 其他常见格式
    pattern3 = re.findall(r'(\d{6})\s*</td>\s*<td[^>]*>\s*([^<]+)', html)

    all_codes = set()
    for code, name in pattern1 + pattern2 + pattern3:
        code = code.strip()
        name = name.strip().replace('&nbsp;','').replace('&amp;','&')
        if len(code) == 6 and code.isdigit():
            all_codes.add((code, name))

    print(f'找到 {len(all_codes)} 个 ETF')

    if all_codes:
        # 保存
        codes_list = sorted(all_codes)
        with open('data/etf_codes.json', 'w', encoding='utf-8') as f:
            json.dump([{'code': c, 'name': n} for c, n in codes_list], f, ensure_ascii=False, indent=2)
        print(f'已保存 {len(codes_list)} 个到 data/etf_codes.json')

        # 打印前20个
        for c, n in codes_list[:20]:
            # 判断市场
            market = 'sh' if c.startswith(('5','6')) else 'sz'
            print(f'  {market}.{c} {n}')
    else:
        # 没匹配到，打印原始HTML前2000字符分析
        print('未匹配到ETF代码，打印HTML片段:')
        print(html[:2000])

except Exception as e:
    print(f'Error: {e}')
