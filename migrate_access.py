# -*- coding: utf-8 -*-
"""migrate_access.py — 新增 access 字段并回填可获取性状态

判定规则（结合已有数据推断，避免全量重测）：
  valid=0                        -> dead（已失效）
  valid IS NULL                  -> unknown（未验活）
  valid=1 且 probe 成功读取       -> direct（能读到目录 = 无需码）
  valid=1 且 needs_code='yes'    -> missing_code（需码且无码）
  valid=1 且 needs_code='no'     -> direct
  valid=1 且 code 非空           -> coded（有码可试）
  valid=1 其他                   -> alive_unk（存活但码状态未知）
"""
import sqlite3, re, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DB = r'H:\zcode-workspace\pan_index.db'
con = sqlite3.connect(DB, timeout=60)
cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')

cols = [c[1] for c in cur.execute('PRAGMA table_info(items)')]
for col in ['access', 'probe_tree', 'tree_at']:
    if col not in cols:
        cur.execute(f'ALTER TABLE items ADD COLUMN {col} TEXT')
        print(f'+ {col}')
con.commit()

# 能从 probe 文本里看出"读到了内容"的标记
READ_OK = re.compile(r'^(✅|🟡|📄|🏆|🕳️|⚠️(?!.*无法))')

rows = cur.execute("""SELECT url, valid, needs_code, code, probe FROM items""").fetchall()
stat = {}
upd = []
for url, valid, nc, code, probe in rows:
    if valid == 0:
        a = 'dead'
    elif valid is None:
        a = 'unknown'
    else:
        probe_ok = bool(probe) and (probe.startswith('✅') or probe.startswith('🟡') or
                                    probe.startswith('📄') or probe.startswith('🏆') or
                                    probe.startswith('🕳️'))
        if probe_ok:
            a = 'direct'          # 成功读到目录 = 无需码
        elif nc == 'yes':
            a = 'missing_code' if not code else 'coded'
        elif nc == 'no':
            a = 'direct'
        elif code:
            a = 'coded'
        else:
            a = 'alive_unk'
    stat[a] = stat.get(a, 0) + 1
    upd.append((a, url))

cur.executemany('UPDATE items SET access=? WHERE url=?', upd)
cur.execute('CREATE INDEX IF NOT EXISTS idx_access ON items(access)')
con.commit()

print('可获取性分布:')
LABEL = {'direct': '✅可直接获取', 'coded': '🔑凭码可获取', 'missing_code': '🔒缺码',
         'alive_unk': '❔存活待确认', 'dead': '❌已失效', 'unknown': '⚪未验证'}
for k, v in sorted(stat.items(), key=lambda x: -x[1]):
    print(f'  {LABEL.get(k,k)}: {v}')
con.close()
