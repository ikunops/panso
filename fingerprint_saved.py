# -*- coding: utf-8 -*-
"""fingerprint_saved.py — 给 saved 表里的夸克链接批量抓内容指纹

复用 content_dedup.py 的 quark_files / content_fingerprint
结果写入 content_fp_saved.json（key=url），可断点续跑
"""
import sqlite3, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content_dedup as CD
import fingerprint_v2 as FP

DB = r'H:\zcode-workspace\pan_index.db'
OUT = r'H:\zcode-workspace\content_fp_saved.json'
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 0

con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
rows = con.execute("""SELECT url, title FROM saved
    WHERE url LIKE '%quark%' AND access<>'dead' ORDER BY saved_at DESC""").fetchall()
con.close()

cache = {}
if os.path.exists(OUT):
    try:
        cache = json.load(open(OUT, encoding='utf-8'))
    except Exception:
        cache = {}
# 合并旧指纹
if os.path.exists('ai_content_fp.json'):
    try:
        old = json.load(open('ai_content_fp.json', encoding='utf-8'))
        for k, v in old.items():
            cache.setdefault(k, v)
    except Exception:
        pass

todo = [(u, t) for u, t in rows if u not in cache]
if LIMIT:
    todo = todo[:LIMIT]
print(f'saved 夸克链接 {len(rows)} 条 | 已有指纹 {len([u for u,_ in rows if u in cache])} | 待抓 {len(todo)}', flush=True)

ok = fail = 0
for i, (url, title) in enumerate(todo, 1):
    try:
        files = CD.quark_files(url, max_dir_calls=120)
        fp = FP.fingerprint(files) if files else None
        if fp:
            cache[url] = fp
            ok += 1
        else:
            cache[url] = None
            fail += 1
    except Exception as e:
        cache[url] = None
        fail += 1
    if i % 20 == 0 or i == len(todo):
        json.dump(cache, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False)
        print(f'  {i}/{len(todo)} | 成功{ok} 失败{fail} | {(title or "")[:34]}', flush=True)
    time.sleep(0.15)

json.dump(cache, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False)
print(f'\n完成: 成功 {ok} 失败 {fail} | 缓存总计 {len(cache)} 条')
