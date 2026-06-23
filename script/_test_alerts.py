import sys; sys.path.insert(0,'.')
from main import app
c = app.test_client()
r = c.get('/api/trading/state')
import json
d = r.get_json()
for h in d['holdings']:
    a = h.get('alerts',{})
    print(f"{h['code']} {h['name']}  现价{h['currentPrice']}  止损{a.get('stopLoss')}  MA10:{a.get('ma10')}  回撤:{a.get('highRetreat')}  量比:{a.get('volRatio')}  背离:{a.get('volDiverge')}")
