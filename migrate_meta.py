# -*- coding: utf-8 -*-
"""migrate_meta.py — 给 items 表补 size_mb / share_time / title_key 字段并回填
可重复运行（幂等）"""
import sqlite3, re, os, datetime

DB = r'H:\zcode-workspace\pan_index.db'
con = sqlite3.connect(DB, timeout=60)
cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')

# 1) 加字段
existing = [c[1] for c in cur.execute('PRAGMA table_info(items)')]
for col, typ in [('size_mb', 'INTEGER'), ('share_time', 'TEXT'), ('title_key', 'TEXT'), ('dup_of', 'TEXT')]:
    if col not in existing:
        cur.execute(f'ALTER TABLE items ADD COLUMN {col} {typ}')
        print(f'+ 新增字段 {col}')
con.commit()

PROBE_MB = re.compile(r'([\d.]+)\s*MB')
PROBE_GB = re.compile(r'([\d.]+)\s*GB')
SIZE_GB = re.compile(r'([\d.]+)\s*G(?:B)?\b', re.I)
SIZE_MB = re.compile(r'([\d.]+)\s*M(?:B)?\b', re.I)
SIZE_KB = re.compile(r'([\d.]+)\s*K(?:B)?\b', re.I)

# 标题归一化：去掉所有非字母数字汉字，小写
def title_key(t):
    if not t:
        return ''
    t = re.sub(r'^(?:\[图片\]|🗣|📚|🆔|⭐️|✅|📺|🀄️|\s)*(?:学习资料|资源名称|资源标题|作品|名称|课程|标题|资源)?\s*[:：]\s*', '', t)
    m = re.search(r'《([^》]{2,90})》', t)
    if m:
        t = m.group(1)
    else:
        t = re.split(r'\s*(?:📚\s*资料介绍|✨\s*作品亮点|作品亮点|资料介绍|资源简介|描述\s*[:：]|简介\s*[:：])', t)[0]
    t = re.sub(r'(带源码课件|带源码|带课件|先保存后观看|避免丢失|持续更新中?|更新中|完结|全\d+集|全集|最新版|最新)', '', t)
    t = re.sub(r'[\s\-_—–·•|｜,，。.、:：;；!！?？()（）\[\]【】《》"\'`~@#$%^&*+=/\\]+', '', t)
    return t.lower()[:80]

# 2) 回填
rows = cur.execute('SELECT url, title, size, probe, first_seen, share_time FROM items').fetchall()
n_size, n_time, n_key = 0, 0, 0
for url, title, size, probe, first_seen, st in rows:
    # 体积：优先 size 字段，其次 probe 里的 MB
    mb = None
    for src in (size or '', probe or ''):
        if not src:
            continue
        g = PROBE_GB.search(src) or SIZE_GB.search(src)
        if g:
            mb = int(float(g.group(1)) * 1024); break
        m = PROBE_MB.search(src) or SIZE_MB.search(src)
        if m:
            mb = int(float(m.group(1))); break
        k = SIZE_KB.search(src)
        if k:
            mb = max(1, int(float(k.group(1)) / 1024)); break
    if mb:
        n_size += 1
    # 分享时间：沿用已有，否则取 first_seen 的日期部分
    if not st:
        st = (first_seen or '')[:10]
        if st:
            n_time += 1
    k = title_key(title)
    if k:
        n_key += 1
    cur.execute('UPDATE items SET size_mb=?, share_time=?, title_key=? WHERE url=?', (mb, st or None, k, url))

cur.execute('CREATE INDEX IF NOT EXISTS idx_sizemb ON items(size_mb)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_sharetime ON items(share_time)')
cur.execute('CREATE INDEX IF NOT EXISTS idx_titlekey ON items(title_key)')
con.commit()
print(f'回填: 体积 {n_size} 条, 时间 {n_time} 条, 去重键 {n_key} 条')

# 3) 标记重复（同 title_key 组内保留最优：有效 > 内容完整 > 体积大 > 收录新）
groups = cur.execute("""SELECT title_key, COUNT(*) FROM items WHERE COALESCE(title_key,'')<>''
                        GROUP BY title_key HAVING COUNT(*)>1""").fetchall()
print(f'重复组: {len(groups)} 组')
cur.execute('UPDATE items SET dup_of=NULL')
dup_cnt = 0
for key, cnt in groups:
    cands = cur.execute("""SELECT url, valid, probe, size_mb, first_seen FROM items WHERE title_key=?
        ORDER BY CASE WHEN valid=1 THEN 0 WHEN valid IS NULL THEN 1 ELSE 2 END,
                 CASE WHEN probe LIKE '✅%' THEN 0 WHEN probe LIKE '🟡%' THEN 1 WHEN probe IS NULL THEN 2
                      WHEN probe LIKE '📄%' THEN 3 ELSE 4 END,
                 COALESCE(size_mb,0) DESC, first_seen DESC""", (key,)).fetchall()
    best = cands[0][0]
    for (u, *_rest) in cands[1:]:
        cur.execute('UPDATE items SET dup_of=? WHERE url=?', (best, u))
        dup_cnt += 1
con.commit()
print(f'标记重复 {dup_cnt} 条（每组保留 1 条主记录）')

tot = cur.execute('SELECT COUNT(*) FROM items').fetchone()[0]
uniq = cur.execute('SELECT COUNT(*) FROM items WHERE dup_of IS NULL').fetchone()[0]
sz = cur.execute('SELECT COUNT(*) FROM items WHERE size_mb IS NOT NULL').fetchone()[0]
print(f'总计 {tot} | 去重后 {uniq} | 有体积 {sz}')
con.close()
