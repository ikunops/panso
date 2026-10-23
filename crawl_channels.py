# -*- coding: utf-8 -*-
"""频道流爬虫：翻频道历史（不限关键词），把网盘链接持续汇入 pan_index.db
用法: python crawl_channels.py --channels a,b,c --pages 30
可反复运行：游标记录在 channel_cursor 表，下次从上次的位置继续往前翻"""
import re, html, json, time, sqlite3, argparse, datetime, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from search_sources import TG_CHANNELS_DEFAULT

DB = r'H:\zcode-workspace\pan_index.db'
ND = {'quark':'夸克','aliyun':'阿里','baidu':'百度','115':'115','uc':'UC','xunlei':'迅雷','189':'天翼','123':'123盘'}
PATTS = [("quark",r"pan\.quark\.cn/s/[0-9a-zA-Z]+"),("aliyun",r"(?:www\.)?(?:alipan|aliyundrive)\.com/s/[0-9a-zA-Z]+"),
 ("baidu",r"pan\.baidu\.com/s/1[0-9a-zA-Z_-]{5,}"),("115",r"(?:115\.com|115cdn\.com|anxia\.com)/s/[0-9a-zA-Z]+"),
 ("uc",r"drive\.uc\.cn/s/[0-9a-zA-Z]+"),("xunlei",r"pan\.xunlei\.com/s/[0-9a-zA-Z]+"),("189",r"cloud\.189\.cn/t/[0-9a-zA-Z]+"),
 ("123",r"(?:123pan|123685|123865)\.com/s/[0-9a-zA-Z-]+")]
CODE_RE = re.compile(r'(?:提取码|访问码|密\s*码|pwd|password|取件码)\s*[:：=]?\s*([0-9a-zA-Z]{4,6})(?![0-9a-zA-Z])', re.I)
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGTP]B)\b", re.I)
MSG_RE = re.compile(r'class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
ID_RE = re.compile(r'data-post="[^"]+?/(\d+)"')
TAG_RE = re.compile(r"<[^>]+>")
BLACK = re.compile(r'游戏|电影|4K|杜比|1080p|2160p|影视|漫画|短剧|综艺', re.I)

ap = argparse.ArgumentParser()
ap.add_argument('--channels', default='')
ap.add_argument('--pages', type=int, default=30, help='每频道翻页数(每页~20条消息)')
ap.add_argument('--no-cursor', action='store_true', help='从最新消息开始扫(用于日常增量)，不读写游标')
args = ap.parse_args()
now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
con = sqlite3.connect(DB, check_same_thread=False, timeout=30); cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')
lock = threading.Lock()
cur.executescript('''CREATE TABLE IF NOT EXISTS items(url TEXT PRIMARY KEY, title TEXT, disk TEXT, code TEXT DEFAULT '',
 size TEXT DEFAULT '', kw TEXT DEFAULT '', source TEXT DEFAULT '', first_seen TEXT, valid INTEGER DEFAULT NULL, needs_code TEXT DEFAULT '');
 CREATE TABLE IF NOT EXISTS channel_cursor(ch TEXT PRIMARY KEY, last_id TEXT, updated TEXT);''')

def fetch(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0","Accept-Language":"zh-CN,zh;q=0.9"})
            with urllib.request.urlopen(req, timeout=15) as r: return r.read().decode("utf-8","ignore")
        except Exception as e:
            if getattr(e,"code",None)==429: time.sleep(5)
            elif i==2: return None
            time.sleep(1)
    return None

added = 0
def crawl(ch):
    global added
    if args.no_cursor:
        before = None
    else:
        with lock:
            row = cur.execute("SELECT last_id FROM channel_cursor WHERE ch=?", (ch,)).fetchone()
        before = row[0] if row else None
    min_id, pages = before, 0
    while pages < args.pages:
        h = fetch(f"https://t.me/s/{ch}" + (f"?before={before}" if before else ""))
        if not h: break
        new_links = []
        for m in MSG_RE.finditer(h):
            raw = m.group(1)
            text = re.sub(r'\s+',' ', TAG_RE.sub(' ', html.unescape(raw))).strip()
            if not text: continue
            codes = CODE_RE.findall(text)
            sizes = SIZE_RE.findall(raw)
            size = ''
            if sizes:
                order = {"KB":0,"MB":1,"GB":2,"TB":3}
                b = max(sizes, key=lambda x:(order.get(x[1].upper(),0), float(x[0])))
                size = f"{b[0]}{b[1].upper()}"
            title = text.split('http')[0].strip(' ：:|，,')[:90]
            if BLACK.search(title + ' ' + text[:60]):  # 影视类不入库
                continue
            for nd, pat in PATTS:
                for u in set(x.group(0) for x in re.finditer(pat, raw, re.I)):
                    url = u if u.startswith('magnet') else 'https://' + u
                    new_links.append((url, title, nd, codes[0] if codes else '', size))
        with lock:
            for url, title, nd, code, size in new_links:
                cur.execute("SELECT 1 FROM items WHERE url=?", (url,))
                if cur.fetchone():
                    if code: cur.execute("UPDATE items SET code=? WHERE url=? AND COALESCE(code,'')=''", (code, url))
                    continue
                cur.execute("INSERT INTO items(url,title,disk,code,size,kw,source,first_seen) VALUES(?,?,?,?,?,?,?,?)",
                            (url, title, ND.get(nd,nd), code, size, '', 'TG流/'+ch, now))
                added += 1
            con.commit()
        ids = [int(x) for x in ID_RE.findall(h)]
        if not ids or (before and min(ids) >= int(before)): break
        before = str(min(ids)); pages += 1
        time.sleep(0.3)
    with lock:
        if not args.no_cursor:
            cur.execute("INSERT INTO channel_cursor(ch,last_id,updated) VALUES(?,?,?) ON CONFLICT(ch) DO UPDATE SET last_id=excluded.last_id, updated=excluded.updated", (ch, str(before or ''), now))
    return ch, pages

chs = [c.strip() for c in args.channels.split(',') if c.strip()] if args.channels else TG_CHANNELS_DEFAULT
with ThreadPoolExecutor(max_workers=3) as ex:
    for ch, pages in ex.map(crawl, chs):
        print(f"  {ch}: 翻了{pages}页", flush=True)
con.commit()
total = cur.execute("SELECT COUNT(*) FROM items").fetchone()[0]
print(f"本次新增 {added} 条, 索引库总计 {total} 条")
con.close()
