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
THS_COOKIE = "_ga=GA1.1.2027773054.1770375736; _clck=1845i53%7C2%7Cg3c%7C0%7C0; __utma=156575163.2027773054.1770375736.1778485076.1778485076.1; __utmz=156575163.1778485076.1.1.utmcsr=cn.bing.com|utmccn=(referral)|utmcmd=referral|utmcct=/; u_ukey=A10702B8689642C6BE607730E11E6E4A; u_uver=1.0.0; u_dpass=K3w6Xz0McVFR3oj5BMTlU0YUh9ky2LrS8gWRDk8XAJWRS0X1I8cDeMRXCIZp2YFzHi80LrSsTFH9a%2B6rtRvqGg%3D%3D; u_did=22FC949D022B4A2E886E5E8F3A2E57A4; u_ttype=WEB; user=MDptb21vaDJuOjpOb25lOjUwMDo4NzgwNTY1Mzc6NywxMTExMTExMTExMSw0MDs0NCwxMSw0MDs2LDEsNDA7NSwxLDQwOzEsMTAxLDQwOzIsMSw0MDszLDEsNDA7NSwxLDQwOzgsMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDEsNDA7MTAyLDEsNDA6Ojo6ODY4MDU2NTM3OjE3Nzg0ODUwOTE6OjoxNzc4NDg1MDgwOjYwNDgwMDowOjE5NjZiMzI5ZDRiNzRiZGFiMTZiZTQ3M2I4NjQ4YjE0ZTpkZWZhdWx0XzU6MQ%3D%3D; userid=868056537; u_name=momoh2n; escapename=momoh2n; ticket=db08ace78dfba770cf3cd0f7e78cbfcb; user_status=1; utk=c12c1d57890971cdf802d5cbfb5b0fbc; sess_tk=eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiIsImtpZCI6InNlc3NfdGtfMSIsImJ0eSI6InNlc3NfdGsifQ.eyJqdGkiOiI0ZWIxNDg4NjNiNDdiZTE2YWJiZDc0NGI5ZDMyNmI5NjEiLCJpYXQiOjE3Nzg0ODUwOTEsImV4cCI6MTc3OTA4OTg5MSwic3ViIjoiODY4MDU2NTM3IiwiaXNzIjoidXBhc3MuMTBqcWthLmNvbS5jbiIsImF1ZCI6IjIwMjAxMTE4NTI4ODkwNzIiLCJhY3QiOiJvZmMiLCJjdWhzIjoiYmRhOThmMDMzOTNkNWVhNjEwZjEzMWU5ZTA0NjkyMDZjN2I4NDdkNWJlOTU4YjUxYjk1NjM2ZGMyMWI3NzEwNiJ9.Vu2o_9COTr6cVZsBx8Ymk97Vet32b3R9Pv25kgUZLqxX1WSbd3h9h532hcAEA-EJB3IeaohNLIo29vxh23cKow; cuc=fpno5u5vq6da; Hm_lvt_69929b9dce4c22a060bd22d703b2a280=1778485093; _ga_H2RK0R0681=GS2.1.s1778485095$o2$g0$t1778485102$j53$l0$h0; __utmb=156575163.2.10.1778485076; Hm_lvt_6dc19a3987135225beb977a0b9931a25=1778485789; HMACCOUNT=2648C61CE52A2BFF; Hm_lvt_9d25c03aef06fec6abea265b79509ba4=1778485789; Hm_lvt_78c58f01938e4d85eaf619eae71b4ed1=1778485076,1778485801; Hm_lpvt_78c58f01938e4d85eaf619eae71b4ed1=1778485801; Hm_lpvt_6dc19a3987135225beb977a0b9931a25=1778485907; Hm_lpvt_9d25c03aef06fec6abea265b79509ba4=1778485907; v=A7DqAMslrrLgmHK20ceUw7pcgXUH-ZS8ttzoR6oIee_evV6rUglk0wbtuMP5"
