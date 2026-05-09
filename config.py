import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# SQLite path under data/
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'stock.db')}"

# Days of daily K-line data to download
DOWNLOAD_DAYS = 300

# BaoStock frequency: "d" (day), "w" (week), "m" (month)
K_FREQUENCY = "d"

# K-line fields: date, open, high, low, close, volume, amount, turnover, pe_ttm
K_FIELDS = "date,open,high,low,close,volume,amount,turn,peTTM"
