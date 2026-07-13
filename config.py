import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# SQLite path under data/
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'stock.db')}"

# Days of daily K-line data to download
DOWNLOAD_DAYS = 5

# BaoStock frequency: "d" (day), "w" (week), "m" (month)
K_FREQUENCY = "d"

# K-line fields: date, open, high, low, close, volume, amount, turnover, pe_ttm
K_FIELDS = "date,open,high,low,close,volume,amount,turn,peTTM"

# 同花顺登录 cookie (登录 q.10jqka.com.cn 后从浏览器 F12 → Application → Cookies 复制)
# 格式: "key1=value1; key2=value2; ..."
THS_COOKIE = "_ga=GA1.1.2027773054.1770375736; _clck=1845i53%7C2%7Cg3c%7C0%7C0; __utma=156575163.2027773054.1770375736.1778485076.1778485076.1; __utmz=156575163.1778485076.1.1.utmcsr=cn.bing.com|utmccn=(referral)|utmcmd=referral|utmcct=/; Hm_lvt_69929b9dce4c22a060bd22d703b2a280=1778485093; _ga_H2RK0R0681=GS2.1.s1778485095$o2$g0$t1778485102$j53$l0$h0; Hm_lvt_78c58f01938e4d85eaf619eae71b4ed1=1778485076,1778485801; _ga_KQBDS1VPQF=GS2.1.s1779696250$o1$g1$t1779696918$j60$l0$h0; u_ukey=A10702B8689642C6BE607730E11E6E4A; u_uver=1.0.0; u_dpass=ZoVf0CFZv0D8DCTWXCvztroiemRMCI%2BrSMuABAw6a0INzfug83VJ1Ah96qiXGZoJHi80LrSsTFH9a%2B6rtRvqGg%3D%3D; u_did=2734E9CDEBE54926BE828AC82400AF40; u_ttype=WEB; ttype=WEB; v=A5uILMiydTem84mC-x2jG4gWKvQAcK17qZRzJo3DcxWwK7XqFUA_wrlUA3ue; user=MDptb18yNTQzNTY5ODI6Ok5vbmU6NTAwOjI2NDM1Njk4Mjo3LDExMTExMTExMTExLDQwOzQ0LDExLDQwOzYsMSw0MDs1LDEsNDA7MSwxMDEsNDA7MiwxLDQwOzMsMSw0MDs1LDEsNDA7OCwwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMSw0MDsxMDIsMSw0MDoyNDo6OjI1NDM1Njk4MjoxNzgzOTI0MjQ1Ojo6MTQzMjY1NTY0MDo2MDQ4MDA6MDoxZjRhNjIzOGFhNTZlNWFiYjBhOGE3ZDI5MTQ5MTlkYjU6ZGVmYXVsdF81OjE%3D; userid=254356982; u_name=mo_254356982; escapename=mo_254356982; ticket=3426a03b1cf1fdb75acc6056e14cd8d8; user_status=0; utk=72db243b838f5322e4436b5b0efe1881; sess_tk=eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiIsImtpZCI6InNlc3NfdGtfMSIsImJ0eSI6InNlc3NfdGsifQ.eyJqdGkiOiJiNTlkOTExNDI5N2Q4YTBhYmI1YTZlYTU4YTIzYTZmNDEiLCJpYXQiOjE3ODM5MjQyNDUsImV4cCI6MTc4NDUyOTA0NSwic3ViIjoiMjU0MzU2OTgyIiwiaXNzIjoidXBhc3MuMTBqcWthLmNvbS5jbiIsImF1ZCI6IjIwMjAxMTE4NTI4ODkwNzIiLCJhY3QiOiJvZmMiLCJjdWhzIjoiNzBiYzM0YmQ1NDllOGZlYzIxMDE1ZGIzZTQyZTY5ZjZhNWFiZDc3ZmNiZmQxN2UxY2UyYWJhMjk5MzQ4YTA3NCJ9.HVzygAjOxUFDSl07gN3Xd5JHv81OJdghZrw578_kapvHZNZq9gzHYRFqL4qnQ2yOoyKJFC_GQWQ56sT-X-f8aw; cuc=oxbi9stuo4s8"