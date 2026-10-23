# -*- coding: utf-8 -*-
"""live_search.py — 实时搜索源（本地库之外的补充）

搜索源:
  local     本地索引库（SQLite LIKE）            —— 始终可用
  pansou    自建 pansou API                     —— 需要实例在线
  tg        Telegram 频道网页搜索（t.me/s/）      —— 需要外网
  pansou.de 公共 pansou 实例（如果可用）
"""
import json, re, os, time, urllib.parse, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'

DEFAULT_SOURCES = {
    'local': {'name': '本地索引库', 'enabled': True, 'kind': 'local',
              'desc': '已收录的 1.4 万条资源，秒级响应'},
    'pansou': {'name': 'pansou 聚合', 'enabled': True, 'kind': 'http',
               'url': os.environ.get('PANSOU_URL', 'http://127.0.0.1:8888/api/search?kw={kw}'),
               'desc': '自建 pansou，聚合 70+ 插件 + 120+ TG 频道'},
    'tg': {'name': 'Telegram 频道', 'enabled': True, 'kind': 'tg',
           'desc': '实时搜 t.me 公开频道（需外网）'},
}

CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'search_sources.json')

# TG 频道池（可在设置里扩展）
TG_CHANNELS = [
    'xuexiziliaobaibaoku', 'xxzlzn', 'xxziliao', 'BooksRealm', 'Q66Share',
    'PanjClub', 'shareAliyun', 'alyp_17362', 'QuarkFree', 'wp123zy',
    'ruanjianfenxiang77', 'tgsearchers3', 'xx123pan', 'quark_res',
]

PATTS = [("quark", r"pan\.quark\.cn/s/[0-9a-zA-Z]+"),
         ("aliyun", r"(?:www\.)?(?:alipan|aliyundrive)\.com/s/[0-9a-zA-Z]+"),
         ("baidu", r"pan\.baidu\.com/s/1[0-9a-zA-Z_-]{5,}"),
         ("115", r"(?:115\.com|115cdn\.com|anxia\.com)/s/[0-9a-zA-Z]+"),
         ("uc", r"drive\.uc\.cn/s/[0-9a-zA-Z]+"),
         ("xunlei", r"pan\.xunlei\.com/s/[0-9a-zA-Z]+"),
         ("189", r"cloud\.189\.cn/t/[0-9a-zA-Z]+"),
         ("123", r"(?:123pan|123685|123865)\.com/s/[0-9a-zA-Z-]+")]
CODE_RE = re.compile(r'(?:提取码|访问码|密\s*码|pwd|password|取件码)\s*[:：=]?\s*([0-9a-zA-Z]{4,6})(?![0-9a-zA-Z])', re.I)
MSG_RE = re.compile(r'class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.S)
TAG_RE = re.compile(r"<[^>]+>")
SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([KMGTP]B)\b", re.I)
DISK_MAP = {'quark': '夸克', 'aliyun': '阿里', 'baidu': '百度', '115': '115',
            'uc': 'UC', 'xunlei': '迅雷', '189': '天翼', '123': '123盘'}


def load_sources():
    if os.path.exists(CFG_PATH):
        try:
            cfg = json.load(open(CFG_PATH, encoding='utf-8'))
            for k, v in cfg.items():
                if k in DEFAULT_SOURCES:
                    DEFAULT_SOURCES[k].update(v)
                else:
                    DEFAULT_SOURCES[k] = v
        except Exception:
            pass
    return DEFAULT_SOURCES


def save_sources(cfg):
    json.dump(cfg, open(CFG_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


# ---------- pansou ----------
def search_pansou(kw, url_tpl, timeout=25):
    out = []
    url = url_tpl.replace('{kw}', urllib.parse.quote(kw))
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
        merged = (d.get('data') or {}).get('merged_by_type') or {}
        for nd, arr in merged.items():
            for it in arr:
                t = (it.get('note') or '').strip()
                u = it.get('url') or ''
                if not t or not u:
                    continue
                out.append({
                    'title': t, 'url': u, 'disk': DISK_MAP.get(nd, nd),
                    'code': (it.get('password') or '').strip(),
                    'size_mb': None, 'share_time': (it.get('datetime') or '')[:10],
                    'source': 'pansou', 'live': True,
                })
    except Exception as e:
        return out, 'pansou 不可用: ' + str(e)[:60]
    return out, None


# ---------- TG ----------
def tg_one(ch, kw, timeout=12):
    out = []
    url = f'https://t.me/s/{ch}?q={urllib.parse.quote(kw)}'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h = r.read().decode('utf-8', 'ignore')
    except Exception:
        return out
    for m in MSG_RE.finditer(h):
        raw = m.group(1)
        text = re.sub(r'\s+', ' ', TAG_RE.sub(' ', __import__('html').unescape(raw))).strip()
        if not text:
            continue
        codes = CODE_RE.findall(text)
        sizes = SIZE_RE.findall(raw)
        size_mb = None
        if sizes:
            order = {'KB': 0, 'MB': 1, 'GB': 2, 'TB': 3}
            b = max(sizes, key=lambda x: (order.get(x[1].upper(), 0), float(x[0])))
            v = float(b[0]); unit = b[1].upper()
            size_mb = int(v / 1024) if unit == 'KB' else (int(v) if unit == 'MB'
                      else int(v * 1024) if unit == 'GB' else int(v * 1048576))
        title = text.split('http')[0].strip(' ：:|，,')[:120]
        for nd, pat in PATTS:
            for u0 in set(x.group(0) for x in re.finditer(pat, raw, re.I)):
                out.append({
                    'title': title, 'url': 'https://' + u0, 'disk': DISK_MAP.get(nd, nd),
                    'code': codes[0] if codes else '', 'size_mb': size_mb,
                    'share_time': time.strftime('%Y-%m-%d'), 'source': f'TG/{ch}', 'live': True,
                })
    return out


def search_tg(kw, channels=None, limit_ch=8, timeout=12):
    chs = (channels or TG_CHANNELS)[:limit_ch]
    res, err = [], None
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(tg_one, c, kw, timeout) for c in chs]
        for f in as_completed(futs):
            try:
                res += f.result()
            except Exception:
                pass
    if not res:
        err = f'TG 无结果或不可达（尝试了 {len(chs)} 个频道）'
    return res, err


# ---------- 统一入口 ----------
def live_search(kw, sources, selected, max_per=120):
    """返回 (items, stats)  items 合并去重；stats 每个源的命中/耗时/错误"""
    items, stats = [], {}
    tasks = []
    for key in selected:
        s = sources.get(key)
        if not s or not s.get('enabled'):
            continue
        if key == 'local':
            continue
        if s['kind'] == 'http':
            tasks.append((key, lambda k=key, u=s.get('url', ''): search_pansou(kw, u)))
        elif s['kind'] == 'tg':
            tasks.append((key, lambda: search_tg(kw, s.get('channels'), s.get('limit_ch', 8))))

    def run(task):
        key, fn = task
        t0 = time.time()
        try:
            res, err = fn()
        except Exception as e:
            res, err = [], str(e)[:80]
        return key, res[:max_per], err, time.time() - t0

    with ThreadPoolExecutor(max_workers=4) as ex:
        for key, res, err, dt in [f.result() for f in [ex.submit(run, t) for t in tasks]]:
            stats[key] = {'count': len(res), 'ms': int(dt * 1000), 'error': err}
            items += res

    # 去重（按 URL）
    seen, uniq = set(), []
    for it in items:
        if it['url'] in seen:
            continue
        seen.add(it['url'])
        uniq.append(it)
    return uniq, stats


if __name__ == '__main__':
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else '软考高项'
    src = load_sources()
    print('搜索源:', {k: v['enabled'] for k, v in src.items()})
    items, stats = live_search(kw, src, ['pansou', 'tg'])
    print('stats:', stats)
    for it in items[:10]:
        print(f"  [{it['disk']}] {it['title'][:50]} | {it['url']}")
