# -*- coding: utf-8 -*-
"""dedupe_by_content.py — 用内容指纹给 saved 表去重（复用 content_dedup + fingerprint_v2）

判定: 同 kind + same_content() 为真 → 同一门课
保留: 推广文件最少 > 有效 > 有码 > 体积大 > 时间新
用法:
    python dedupe_by_content.py           # 预演
    python dedupe_by_content.py --apply   # 执行
"""
import sqlite3, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fingerprint_v2 as FP

DB = r'H:\zcode-workspace\pan_index.db'
FP_PATH = r'H:\zcode-workspace\content_fp_saved.json'
APPLY = '--apply' in sys.argv

con = sqlite3.connect(DB, timeout=30)
cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')

fps = {}
if os.path.exists(FP_PATH):
    fps = json.load(open(FP_PATH, encoding='utf-8'))

rows = cur.execute("SELECT url,title,disk,code,size_mb,access,saved_at FROM saved").fetchall()
print(f'saved 共 {len(rows)} 条 | 有指纹 {sum(1 for u,*_ in rows if fps.get(u))} 条')

# 按指纹聚类
items = []
for url, title, disk, code, size, access, at in rows:
    items.append({'url': url, 'title': title, 'disk': disk, 'code': code,
                  'size': size, 'access': access, 'at': at, 'fp': fps.get(url)})

withfp = [x for x in items if x['fp']]
nofp = [x for x in items if not x['fp']]
print(f'可参与内容去重: {len(withfp)} | 无指纹(跳过): {len(nofp)}')

# O(n^2) 聚类，但按 kind 分桶 + 先按 video_count/doc_count 粗筛
from collections import defaultdict
buckets = defaultdict(list)
for x in withfp:
    fp = x['fp']
    k = fp.get('kind')
    n = fp.get('video_count') if k == 'video' else fp.get('doc_count')
    buckets[(k, n)].append(x)

clusters, seen = [], set()
for key, group in buckets.items():
    for i, x in enumerate(group):
        if x['url'] in seen:
            continue
        cl = [x]
        seen.add(x['url'])
        for y in group[i + 1:]:
            if y['url'] in seen:
                continue
            if FP.same_content(x['fp'], y['fp'], x.get('title',''), y.get('title','')):
                cl.append(y)
                seen.add(y['url'])
        if len(cl) > 1:
            clusters.append(cl)

to_delete = []
for cl in clusters:
    cl = FP.pick_best(cl)      # 漏斗择优
    for d in cl[1:]:
        to_delete.append(d)

print(f'\n内容重复组 {len(clusters)} 组 | 可删除 {len(to_delete)} 条')
print('\n=== 重复组明细（前 20 组）===')
for cl in sorted(clusters, key=lambda c: -len(c))[:20]:
    keep = cl[0]
    print(f'\n[{len(cl)} 个] ✔ 保留: {keep["title"][:58]}')
    print(f'         推广{keep["fp"].get("promo",0)} 文件{keep["fp"].get("file_count","?")} {keep["url"]}')
    for d in cl[1:]:
        print(f'        ✘ 删: {d["title"][:58]}  (推广{d["fp"].get("promo",0)})')

if APPLY:
    for d in to_delete:
        cur.execute('DELETE FROM saved WHERE url=?', (d['url'],))
    con.commit()
    print(f'\n已删除 {len(to_delete)} 条 | 剩余 {cur.execute("SELECT COUNT(*) FROM saved").fetchone()[0]} 条')
else:
    print('\n[预演] 加 --apply 执行')
con.close()
