# -*- coding: utf-8 -*-
"""复检索引库链接有效性 + 内容体检
用法:
    python revalidate.py [--limit N] [--stale-days D] [--probe-limit M] [--probe-only]
验活: 更新 valid/checked_at
体检: 对有效但未体检过的链接探测目录结构, 写入 probe/probe_at (默认每轮最多 M 条, 避免超时)"""
import sys, os, time, asyncio, sqlite3, datetime, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from NetDiskLinkValidator import check_url, extract_share_id
from verify_access import verify_url, LABELS
import content_probe

DB = r'H:\zcode-workspace\pan_index.db'
ap = argparse.ArgumentParser()
ap.add_argument('--limit', type=int, default=0, help='本次最多复检条数, 0=不限')
ap.add_argument('--stale-days', type=int, default=7, help='多少天前的检查结果视为过期')
ap.add_argument('--probe-limit', type=int, default=150, help='本轮最多内容体检条数, 0=跳过体检')
ap.add_argument('--probe-only', action='store_true', help='只做内容体检，不做验活')
a = ap.parse_args()

now = datetime.datetime.now()
now_s = now.strftime('%Y-%m-%d %H:%M')
cutoff = (now - datetime.timedelta(days=a.stale_days)).strftime('%Y-%m-%d %H:%M')

con = sqlite3.connect(DB, timeout=30, check_same_thread=False)
cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')

def ensure_col(coldef):
    name = coldef.split()[0]
    cols = [c[1] for c in cur.execute('PRAGMA table_info(items)')]
    if name in cols:
        return True
    for i in range(300):
        try:
            cur.execute(f'ALTER TABLE items ADD COLUMN {coldef}')
            con.commit()
            return True
        except Exception as e:
            if 'duplicate column' in str(e):
                return True
            time.sleep(2)
            if i % 30 == 29:
                print(f'  等待锁释放中... ({i*2}s)', flush=True)
    return False

for cd in ['checked_at TEXT', 'probe TEXT', 'probe_at TEXT']:
    if not ensure_col(cd):
        print(f'缺少字段 {cd} 且无法创建，退出', flush=True)
        sys.exit(1)

# ---------------- 阶段1: 验活 ----------------
if not a.probe_only:
    sql = ("SELECT url FROM items WHERE url NOT LIKE 'magnet%' "
           "AND (checked_at IS NULL OR checked_at < ?) "
           "ORDER BY (checked_at IS NULL) DESC, checked_at ASC")
    if a.limit:
        sql += f" LIMIT {a.limit}"
    todo = [r[0] for r in cur.execute(sql, (cutoff,))
            if extract_share_id(r[0]) and not r[0].startswith('magnet:')]
    print(f"[验活] 待复检 {len(todo)} 条（截止 {cutoff}）", flush=True)

    sem = asyncio.Semaphore(4)
    done, valid_n = 0, 0
    lock = asyncio.Lock()
    # 需要码的条目带上库里的码一起验（可判定码是否正确）
    codemap = {r[0]: (r[1] or '') for r in cur.execute("SELECT url, code FROM items WHERE url NOT LIKE 'magnet%'")}
    async def chk(u):
        global done, valid_n
        async with sem:
            st, detail = 'unknown', {}
            for attempt in range(3):
                try:
                    st, detail = await asyncio.to_thread(verify_url, u, codemap.get(u, ''))
                    if st != 'unknown': break
                except Exception as e:
                    st, detail = 'unknown', {'err': str(e)[:40]}
                await asyncio.sleep(2.5 * (attempt + 1))
            async with lock:
                is_alive = 1 if st != 'dead' and st != 'unknown' else 0
                need = 'yes' if st in ('no_code', 'bad_code', 'with_code') else ('no' if st == 'accessible' else None)
                cur.execute("UPDATE items SET valid=?, access=?, needs_code=COALESCE(?,needs_code), checked_at=? WHERE url=?",
                            (is_alive, st, need, now_s, u))
                done += 1
                if st in ('accessible', 'with_code'): valid_n += 1
                if done % 100 == 0:
                    con.commit()
                    print(f"  进度 {done}/{len(todo)} 可获取 {valid_n}", flush=True)
    async def run():
        await asyncio.gather(*[chk(u) for u in todo])
    if todo:
        asyncio.run(run())
        con.commit()
    print(f"[验活] 完成: {done} 条, 可获取 {valid_n}", flush=True)
    for k, v in cur.execute("SELECT access, COUNT(*) FROM items WHERE checked_at=? GROUP BY access", (now_s,)).fetchall():
        print(f"    {LABELS.get(k, k)}: {v}", flush=True)

# ---------------- 阶段2: 内容体检 ----------------
if a.probe_limit > 0:
    psql = ("SELECT url, title, code FROM items "
            "WHERE valid=1 AND url NOT LIKE 'magnet%' "
            "AND (probe_at IS NULL OR probe_at < ?) "
            "ORDER BY (probe_at IS NULL) DESC, probe_at ASC LIMIT ?")
    pc = (now - datetime.timedelta(days=30)).strftime('%Y-%m-%d %H:%M')
    p_todo = cur.execute(psql, (pc, a.probe_limit)).fetchall()
    print(f"[体检] 待检测 {len(p_todo)} 条", flush=True)
    p_ok, p_frag, p_err = 0, 0, 0
    for i, (url, title, code) in enumerate(p_todo, 1):
        if 'quark' not in url and 'baidu' not in url:
            cur.execute("UPDATE items SET probe=?, probe_at=? WHERE url=?", ('⚪不支持体检', now_s, url))
            continue
        try:
            r = content_probe.probe(url, code or '')
            v = content_probe.verdict(r, title or '')
        except Exception as e:
            r, v = {'ok': False, 'err': str(e)[:40]}, '❓探测异常'
        cur.execute("UPDATE items SET probe=?, probe_at=? WHERE url=?", (v[:120], now_s, url))
        if v.startswith('✅') or v.startswith('📄'): p_ok += 1
        elif v.startswith('⚠️'): p_frag += 1
        elif '❓' in v: p_err += 1
        if i % 20 == 0:
            con.commit()
            print(f"  进度 {i}/{len(p_todo)} | 完整{p_ok} 碎片{p_frag} 异常{p_err}", flush=True)
        time.sleep(0.4)
    con.commit()
    print(f"[体检] 完成: 完整 {p_ok} | 碎片 {p_frag} | 异常 {p_err}", flush=True)

total = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
v = cur.execute("SELECT COUNT(*) FROM items WHERE valid=1").fetchone()[0]
pv = cur.execute("SELECT COUNT(*) FROM items WHERE probe IS NOT NULL").fetchone()[0]
print(f"库内总计 {total} | 有效 {v} | 已体检 {pv}", flush=True)
con.close()
