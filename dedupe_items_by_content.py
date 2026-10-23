# -*- coding: utf-8 -*-
"""dedupe_items_by_content.py — 用内容指纹给 items 表去重

标记方式: 写 items.dup_of（指向保留的 url），不物理删除
保留: 推广少 > 有效 > 有码 > 体积大 > 收录新

用法:
    python dedupe_items_by_content.py           # 预演
    python dedupe_items_by_content.py --apply   # 执行
"""
import sqlite3, json, os, sys
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fingerprint_v2 as FP

DB = r'H:\zcode-workspace\pan_index.db'
FP_PATH = r'H:\zcode-workspace\content_fp_items.json'
APPLY = '--apply' in sys.argv

fps = {}
if os.path.exists(FP_PATH):
    fps = json.load(open(FP_PATH, encoding='utf-8'))
# 合并 saved 的指纹
for f in ['content_fp_saved.json', 'ai_content_fp.json']:
    if os.path.exists(f):
        try:
            d = json.load(open(f, encoding='utf-8'))
            for k, v in d.items():
                if v:
                    fps.setdefault(k, v)
        except Exception:
            pass

con = sqlite3.connect(DB, timeout=60)
cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')

rows = cur.execute("""SELECT url,title,disk,code,size_mb,access,first_seen
                      FROM items WHERE dup_of IS NULL AND url LIKE '%quark%'""").fetchall()
print(f'items 去重后夸克 {len(rows)} 条 | 有指纹 {sum(1 for r in rows if fps.get(r[0]))} 条')

items = [{'url': r[0], 'title': r[1], 'disk': r[2], 'code': r[3],
          'size': r[4], 'access': r[5], 'at': r[6], 'fp': fps.get(r[0])} for r in rows]
withfp = [x for x in items if x['fp']]
print(f'可参与内容去重: {len(withfp)} | 跳过: {len(items)-len(withfp)}')

# 按 kind + 数量分桶（粗筛，避免 O(n^2)）
buckets = defaultdict(list)
for x in withfp:
    fp = x['fp']
    k = fp.get('kind')
    n = fp.get('video_count') if k == 'video' else fp.get('doc_count')
    buckets[(k, n)].append(x)

clusters, seen = [], set()
for key, group in buckets.items():
    if len(group) < 2:
        continue
    for i, x in enumerate(group):
        if x['url'] in seen:
            continue
        cl = [x]; seen.add(x['url'])
        for y in group[i+1:]:
            if y['url'] in seen:
                continue
            if FP.same_content(x['fp'], y['fp'], x.get('title',''), y.get('title','')):
                cl.append(y); seen.add(y['url'])
        if len(cl) > 1:
            clusters.append(cl)

to_mark = []
for cl in clusters:
    cl = FP.pick_best(cl)      # 漏斗择优
    keep = cl[0]
    for d in cl[1:]:
        to_mark.append((d['url'], keep['url'], d['title'], keep['title']))

print(f'\n内容重复组 {len(clusters)} 组 | 可标记 {len(to_mark)} 条\n')
print('=== 重复最多的 15 组 ===')
for cl in sorted(clusters, key=lambda c: -len(c))[:15]:
    print(f'\n[{len(cl)} 个] ✔ {cl[0]["title"][:56]}')
    for d in cl[1:6]:
        print(f'        ✘ {d["title"][:56]}')

if APPLY:
    for dup_url, keep_url, *_ in to_mark:
        cur.execute("UPDATE items SET dup_of=? WHERE url=?", (keep_url, dup_url))
    con.commit()
    total = cur.execute('SELECT COUNT(*) FROM items WHERE dup_of IS NULL').fetchone()[0]
    print(f'\n已标记 {len(to_mark)} 条重复 | 去重后剩余 {total} 条')
else:
    print('\n[预演] 加 --apply 执行')
con.close()
