# -*- coding: utf-8 -*-
"""fingerprint_items.py — 给 items 表全量抓内容指纹（可断点续跑）

只抓: 去重后 + 未失效 + 夸克链接
复用 fingerprint_v2.fingerprint（视频/文档双指纹）
结果: content_fp_items.json
"""
import sqlite3, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content_dedup as CD
import fingerprint_v2 as FP

FILES_OUT = r'H:\zcode-workspace\content_files_items.json'

DB = r'H:\zcode-workspace\pan_index.db'
OUT = r'H:\zcode-workspace\content_fp_items.json'
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 0
SKIP_DEAD = '--include-dead' not in sys.argv

con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
sql = "SELECT url, title FROM items WHERE dup_of IS NULL AND url LIKE '%quark%'"
if SKIP_DEAD:
    sql += " AND (access <> 'dead' OR access IS NULL)"
sql += " ORDER BY CASE WHEN access='accessible' THEN 0 ELSE 1 END"
rows = con.execute(sql).fetchall()
con.close()

cache = {}
if os.path.exists(OUT):
    try:
        cache = json.load(open(OUT, encoding='utf-8'))
    except Exception:
        cache = {}
# 复用已有指纹（saved 和 AI 那批）
for f in ['content_fp_saved.json', 'ai_content_fp.json']:
    if os.path.exists(f):
        try:
            d = json.load(open(f, encoding='utf-8'))
            for k, v in d.items():
                if v:
                    cache.setdefault(k, v)
        except Exception:
            pass

todo = [(u, t) for u, t in rows if u not in cache]
if LIMIT:
    todo = todo[:LIMIT]
print(f'items 夸克(未失效) {len(rows)} 条 | 已有指纹 {len(rows)-len(todo)} | 待抓 {len(todo)}', flush=True)

file_cache = {}
if os.path.exists(FILES_OUT):
    try:
        file_cache = json.load(open(FILES_OUT, encoding='utf-8'))
    except Exception:
        file_cache = {}

ok = fail = 0
t0 = time.time()
for i, (url, title) in enumerate(todo, 1):
    try:
        files = CD.quark_files(url, max_dir_calls=100)
        fp = FP.fingerprint(files) if files else None
        cache[url] = fp
        if files:
            file_cache[url] = [{'n': f['name'], 's': f['size']} for f in files]
        if fp:
            ok += 1
        else:
            fail += 1
    except Exception:
        cache[url] = None
        fail += 1
    if i % 50 == 0 or i == len(todo):
        json.dump(cache, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False)
        json.dump(file_cache, open(FILES_OUT, 'w', encoding='utf-8'), ensure_ascii=False)
        el = time.time() - t0
        eta = (len(todo) - i) / max(i / el, 0.01) / 60
        print(f'  {i}/{len(todo)} | 成功{ok} 失败{fail} | {i/el:.1f}/s | 剩余~{eta:.0f}分 | {(title or "")[:28]}', flush=True)
    time.sleep(0.1)

json.dump(cache, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False)
print(f'\n完成: 成功 {ok} 失败 {fail} | 缓存总计 {len(cache)} 条', flush=True)
