import baostock as bs
bs.login()
test_codes = ['sh.510050','sh.510300','sh.510500','sh.512100','sh.513100',
              'sh.515050','sh.516880','sh.518880','sh.560010','sh.588000',
              'sz.159915','sz.159995','sh.511010','sh.517180','sh.562800',
              'sz.159865','sz.159766','sh.588200','sh.511260','sh.510880']
ok = 0
for code in test_codes:
    rs = bs.query_history_k_data_plus(code, 'date,close', start_date='2026-07-01', end_date='2026-07-01', frequency='d', adjustflag='3')
    rows = []
    while rs.next(): rows.append(rs.get_row_data())
    if rows: print(f'{code}: OK'); ok += 1
    else: print(f'{code}: --')
print(f'{ok}/{len(test_codes)}')
bs.logout()
