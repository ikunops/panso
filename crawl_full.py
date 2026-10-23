# -*- coding: utf-8 -*-
"""crawl_full.py — 全网频道全量爬取（112 频道 × 深翻）

与 crawl_channels.py 的区别:
  - 使用游标（断点续爬），不会重复抓
  - 频道列表从 search_sources 读，自动同步
  - 分批输出进度，支持 Ctrl+C 中断后继续
  - 深度爬取（默认 40 页 ≈ 800 条消息/频道）

用法:
    python crawl_full.py                # 全量（断点续爬）
    python crawl_full.py --pages 60     # 更深
    python crawl_full.py --channels a,b # 指定频道
"""
import re, html, json, time, sqlite3, argparse, datetime, threading, urllib.request, urllib.parse, sys, os
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from search_sources import TG_CHANNELS_DEFAULT

DB = r'H:\zcode-workspace\pan_index.db'
ND = {'quark': '夸克', 'aliyun': '阿里', 'baidu': '百度', '115': '115', 'uc': 'UC',
      'xunlei': '迅雷', '189': '天翼', '123': '123盘'}
PATTS = [("quark", r"pan\.quark\.cn/s/[0-9a-zA-Z]+"),
         ("aliyun", r"(?:www\.)?(?:alipan|aliyundrive)\.com/s/[0-9a-zA-Z]+"),
         ("baidu", r"pan\.baidu\.com/s/1[0-9a-zA-Z_-]{5,}"),
         ("115", r"(?:115\.com|115cdn\.com|anxia\.com)/s/[0-9a-zA-Z]+"),
         ("uc", r"drive\.uc\.cn/s/[0-9a-zA-Z]+"),
         ("xunlei", r"pan\.xunlei\.com/s/[0-9a-zA-Z]+"),
         ("189", r"cloud\.189\.cn/t/[0-9a-zA-Z]+"),
         ("123", r"(?:123pan|123685|123865)\.com/s/[0-9a-zA-Z-]+")]
CODE_RE = re.compile(r'(?:提取码|访问码|密\s*码|pwd|password|取件码)\s*[:：=]?\s*([0-9a-zA-Z]{4,6})(?![0-9a-zA-Z])', re.I)
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGTP]B)\b", re.I)
MSG_RE = re.compile(r'class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
ID_RE = re.compile(r'data-post="[^"]+?/(\d+)"')
TAG_RE = re.compile(r"<[^>]+>")
# 只排除纯影视（电影/剧集），保留教程/课程/资料
BLACK = re.compile(r'(电影|4K|杜比视界|1080p|2160p|REMUX|蓝光原盘)', re.I)

ap = argparse.ArgumentParser()
ap.add_argument('--channels', default='')
ap.add_argument('--pages', type=int, default=40)
ap.add_argument('--workers', type=int, default=4)
a = ap.parse_args()

chs = [c.strip() for c in a.channels.split(',') if c.strip()] if a.channels else TG_CHANNELS_DEFAULT
print(f'目标频道 {len(chs)} 个 | 每频道最多 {a.pages} 页（≈{a.pages*20} 条消息）', flush=True)

con = sqlite3.connect(DB, timeout=60, check_same_thread=False)
cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')
cur.executescript('''CREATE TABLE IF NOT EXISTS items(
 url TEXT PRIMARY KEY, title TEXT, disk TEXT, code TEXT DEFAULT '', size TEXT DEFAULT '',
 kw TEXT DEFAULT '', source TEXT DEFAULT '', first_seen TEXT, valid INTEGER DEFAULT NULL,
 needs_code TEXT DEFAULT '', checked_at TEXT, probe TEXT, probe_at TEXT, size_mb INTEGER,
 share_time TEXT, title_key TEXT, dup_of TEXT, access TEXT);
 CREATE TABLE IF NOT EXISTS channel_cursor(ch TEXT PRIMARY KEY, last_id TEXT, updated TEXT);''')
lock = threading.Lock()

def fetch(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0',
                'Accept-Language': 'zh-CN,zh;q=0.9'})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode('utf-8', 'ignore')
        except Exception as e:
            if getattr(e, 'code', None) == 429:
                time.sleep(8)
            elif i == 2:
                return None
            time.sleep(2)
    return None

stats = {'added': 0, 'dup': 0, 'pages': 0, 'fail': 0}

def crawl(ch):
    with lock:
        row = cur.execute('SELECT last_id FROM channel_cursor WHERE ch=?', (ch,)).fetchone()
    before = row[0] if row else None
    pages = 0
    local_add = 0
    while pages < a.pages:
        url = f'https://t.me/s/{ch}' + (f'?before={before}' if before else '')
        h = fetch(url)
        if not h:
            with lock: stats['fail'] += 1
            break
        batch = []
        for m in MSG_RE.finditer(h):
            raw = m.group(1)
            text = re.sub(r'\s+', ' ', TAG_RE.sub(' ', html.unescape(raw))).strip()
            if not text:
                continue
            title = text.split('http')[0].strip(' ：:|，,')[:100]
            if not title or BLACK.search(title):
                continue
            codes = CODE_RE.findall(text)
            sizes = SIZE_RE.findall(raw)
            size = ''
            if sizes:
                order = {'KB': 0, 'MB': 1, 'GB': 2, 'TB': 3}
                b = max(sizes, key=lambda x: (order.get(x[1].upper(), 0), float(x[0])))
                size = f'{b[0]}{b[1].upper()}'
            for nd, pat in PATTS:
                for u0 in set(x.group(0) for x in re.finditer(pat, raw, re.I)):
                    batch.append(('https://' + u0, title, nd, codes[0] if codes else '', size))
        with lock:
            for u, title, nd, code, size in batch:
                if cur.execute('SELECT 1 FROM items WHERE url=?', (u,)).fetchone():
                    if code:
                        cur.execute("UPDATE items SET code=? WHERE url=? AND COALESCE(code,'')=''", (code, u))
                    stats['dup'] += 1
                    continue
                cur.execute('''INSERT INTO items(url,title,disk,code,size,kw,source,first_seen,access)
                               VALUES(?,?,?,?,?,?,?,?,?)''',
                            (u, title, ND.get(nd, nd), code, size, '', f'全量流/{ch}',
                             datetime.datetime.now().strftime('%Y-%m-%d %H:%M'), 'unknown'))
                stats['added'] += 1
                local_add += 1
            con.commit()
            stats['pages'] += 1
        ids = [int(x) for x in ID_RE.findall(h)]
        if not ids or (before and min(ids) >= int(before)):
            break
        before = str(min(ids))
        pages += 1
        time.sleep(0.4)
    with lock:
        cur.execute('''INSERT INTO channel_cursor(ch,last_id,updated) VALUES(?,?,?)
                       ON CONFLICT(ch) DO UPDATE SET last_id=excluded.last_id, updated=excluded.updated''',
                    (ch, str(before or ''), datetime.datetime.now().strftime('%Y-%m-%d %H:%M')))
        con.commit()
    print(f'  [{ch}] {pages}页 +{local_add}条', flush=True)
    return local_add

t0 = time.time()
with ThreadPoolExecutor(max_workers=a.workers) as ex:
    list(ex.map(crawl, chs))

total = cur.execute('SELECT COUNT(*) FROM items').fetchone()[0]
print(f'\n完成: 新增 {stats["added"]} | 重复 {stats["dup"]} | 翻页 {stats["pages"]} | 失败频道 {stats["fail"]}')
print(f'耗时 {(time.time()-t0)/60:.1f} 分钟 | 库总量 {total}')
con.close()
