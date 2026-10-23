# -*- coding: utf-8 -*-
"""search_sources.py — 可扩展的搜索源引擎（度盘搜式插件模型）

每个源 = 一段 JSON 配置：
    {
      "name": "显示名",
      "kind": "json",                       # json | tg | pansou
      "url": "https://site/api/search?kw={kw}",
      "headers": {...},
      "list_path": "data.items",            # 结果数组在响应里的路径
      "fields": {"title":"title","url":"url","code":"code","time":"is_time","disk":"category"}
    }

加一个新源 = 加一条配置（界面上可编辑，存 search_sources.json）。
"""
import json, os, re, time, html as H, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36'
CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'search_sources.json')

DISK_MAP = {'quark': '夸克', 'aliyun': '阿里', 'baidu': '百度', '115': '115',
            'uc': 'UC', 'xunlei': '迅雷', '189': '天翼', '123': '123盘'}
DISK_HINT = [('quark', '夸克'), ('alipan', '阿里'), ('aliyundrive', '阿里'),
             ('baidu', '百度'), ('xunlei', '迅雷'), ('uc.cn', 'UC'),
             ('189.cn', '天翼'), ('123pan', '123盘'), ('115.com', '115')]

TG_CHANNELS_DEFAULT = [
    "xuexiziliaobaibaoku",
    "xxzlzn",
    "xxziliao",
    "BooksRealm",
    "Q66Share",
    "PanjClub",
    "shareAliyun",
    "alyp_17362",
    "QuarkFree",
    "wp123zy",
    "ruanjianfenxiang77",
    "tgsearchers3",
    "xx123pan",
    "quark_res",
    "tgsearchers7",
    "Aliyun_4K_Movies",
    "yunpanx",
    "yp123pan",
    "yunpanxunlei",
    "tianyifc",
    "peccxinpd",
    "gotopan",
    "baicaoZY",
    "MCPH02",
    "MCPH03",
    "bdwpzhpd",
    "ucwpzy",
    "Quark_Movies",
    "XiangxiuNBB",
    "ucquark",
    "yingshifenxiang123",
    "zyfb123",
    "Lsp115",
    "taoxgzy",
    "Channel_Shares_115",
    "vip115hot",
    "yunpan139",
    "yunpan189",
    "yunpanuc",
    "yydf_hzl",
    "leoziyuan",
    "yoyokuakeduanju",
    "TG654TG",
    "QukanMovie",
    "yeqingjie_GJG666",
    "movielover8888_film3",
    "Baidu_netdisk",
    "D_wusun",
    "FLMdongtianfudi",
    "KaiPanshare",
    "rjyxfx",
    "PikPak_Share_Channel",
    "newproductsourcing",
    "yunpanNB",
    "kkdj001",
    "pxyunpanxunlei",
    "jxwpzy",
    "kuakedongman",
    "xiangnikanj",
    "solidsexydoll",
    "guoman4K",
    "zdqxm",
    "kduanju",
    "cilidianying",
    "CBduanju",
    "SharePanFilms",
    "dzsgx",
    "douerpan",
    "Netdisk_Movies",
    "yunpanquark",
    "ciliziyuanku",
    "jzmm_123pan",
    "wpan8",
    "mqte5",
    "regengguangya",
    "regeng115",
    "regeng123",
    "yy80986098",
    "pan_guangya",
    "guangyapan_episode",
    "guangya_hdhive",
    "guangyapindao",
    "domgmingapk",
    "dianying4k",
    "tgbokee",
    "ucshare",
    "gokuapan",
    "WFYSFX03",
    "gimy100",
    "gimy115iso",
    "fcij5",
    "xvth5",
    "phzvip",
    "jdbigdiscount",
    "youxigs",
    "zhoulanziyuan",
    "seedhub_pro",
    "jnjy_5",
    "wpzyk",
    "jpnd5",
    "XunLeiPinDao",
    "a123fxme",
    "WPpindao",
    "kuyupan",
    "djya5",
    "zh_vip",
    "gdsharing",
    "guangyaya2026",
    "baidyunpan",
    "yunpans",
    "rbzhwpzy",
    "Panzi88com"
]

# 默认 pansou 实例地址，可通过环境变量覆盖
PANSOU_URL = os.environ.get('PANSOU_URL', 'http://127.0.0.1:8888/api/search?kw={kw}')

# ============ 预置源 ============
DEFAULT_SOURCES = {
    'local': {
        'name': '本地索引库', 'kind': 'local', 'enabled': True,
        'desc': '已收录的资源（秒级，离线可用）',
    },
    'pansou': {
        'name': 'pansou（自建）', 'kind': 'pansou', 'enabled': True,
        'url': PANSOU_URL,
        'desc': '自建实例，聚合 70+ 插件与 120+ TG 频道',
    },
    'kkso': {
        'name': 'kkso 夸克搜（无效接口，已停用）', 'kind': 'json', 'enabled': False,
        'url': 'https://kkso.net/api/search?kw={kw}',
        'list_path': 'data.items',
        'fields': {'title': 'title', 'url': 'url', 'code': 'code',
                   'time': 'is_time', 'disk': 'category'},
        'desc': '公共源，夸克+百度，JSON 接口稳定',
    },
    'tg': {
        'name': 'Telegram 频道', 'kind': 'tg', 'enabled': True,
        'limit_ch': 20, 'desc': '实时检索 t.me 公开频道',
    },
}


def load_sources():
    cfg = json.loads(json.dumps(DEFAULT_SOURCES))  # deep copy
    if os.path.exists(CFG_PATH):
        try:
            saved = json.load(open(CFG_PATH, encoding='utf-8'))
            for k, v in saved.items():
                if k in cfg:
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def save_sources(cfg):
    json.dump(cfg, open(CFG_PATH, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


def _dig(obj, path):
    cur = obj
    for part in path.split('.'):
        if cur is None:
            return None
        if part.isdigit() and isinstance(cur, list):
            i = int(part)
            cur = cur[i] if i < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _guess_disk(url, hint=''):
    u = (url or '').lower()
    for key, name in DISK_HINT:
        if key in u:
            return name
    h = (hint or '').strip()
    return DISK_MAP.get(h, h) if h else '?'


# ---------- 通用 JSON 源 ----------
def search_json(kw, src, timeout=15, max_items=60):
    url = src['url'].replace('{kw}', urllib.parse.quote(kw))
    headers = {'User-Agent': UA}
    headers.update(src.get('headers') or {})
    out = []
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'ignore')
        data = json.loads(raw)
    except Exception as e:
        return [], f'{src.get("name", "源")} 不可用: {str(e)[:50]}'

    items = _dig(data, src.get('list_path', 'data.items')) or []
    if not isinstance(items, list):
        return [], '响应结构与配置不符'
    fm = src.get('fields') or {}
    for it in items[:max_items]:
        if not isinstance(it, dict):
            continue
        title = str(_dig(it, fm.get('title', 'title')) or '').strip()
        url_ = str(_dig(it, fm.get('url', 'url')) or '').strip()
        if not title or not url_:
            continue
        if not url_.startswith('http'):
            url_ = 'https://' + url_.lstrip('/')
        out.append({
            'title': title[:200], 'url': url_,
            'disk': _guess_disk(url_, str(_dig(it, fm.get('disk', 'category')) or '')),
            'code': str(_dig(it, fm.get('code', 'code')) or '').strip(),
            'size_mb': None,
            'share_time': str(_dig(it, fm.get('time', 'time')) or '')[:10],
            'source': src.get('name', 'json'), 'live': True,
        })
    return out, (None if out else '无结果')


# ---------- pansou（自家实例，结构固定） ----------
def search_pansou(kw, url_tpl, timeout=25):
    try:
        url = url_tpl.replace('{kw}', urllib.parse.quote(kw))
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode('utf-8', 'ignore'))
        out = []
        merged = (d.get('data') or {}).get('merged_by_type') or {}
        for nd, arr in merged.items():
            for it in arr:
                t = (it.get('note') or '').strip()
                u = it.get('url') or ''
                if not t or not u:
                    continue
                out.append({'title': t[:200], 'url': u,
                            'disk': DISK_MAP.get(nd, nd),
                            'code': (it.get('password') or '').strip(),
                            'size_mb': None,
                            'share_time': (it.get('datetime') or '')[:10],
                            'source': 'pansou', 'live': True})
        return out, (None if out else 'pansou 无结果')
    except Exception as e:
        return [], 'pansou 不可用: ' + str(e)[:50]


# ---------- TG ----------
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


def _tg_one(ch, kw, timeout=12):
    out = []
    try:
        url = f'https://t.me/s/{ch}?q={urllib.parse.quote(kw)}'
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'zh-CN,zh;q=0.9'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h = r.read().decode('utf-8', 'ignore')
    except Exception:
        return out
    for m in MSG_RE.finditer(h):
        raw = m.group(1)
        text = re.sub(r'\s+', ' ', TAG_RE.sub(' ', H.unescape(raw))).strip()
        if not text:
            continue
        codes = CODE_RE.findall(text)
        sizes = SIZE_RE.findall(raw)
        size_mb = None
        if sizes:
            order = {'KB': 0, 'MB': 1, 'GB': 2, 'TB': 3}
            b = max(sizes, key=lambda x: (order.get(x[1].upper(), 0), float(x[0])))
            v, u = float(b[0]), b[1].upper()
            size_mb = int(v / 1024) if u == 'KB' else (int(v) if u == 'MB'
                      else int(v * 1024) if u == 'GB' else int(v * 1048576))
        title = text.split('http')[0].strip(' ：:|，,')[:120]
        for nd, pat in PATTS:
            for u0 in set(x.group(0) for x in re.finditer(pat, raw, re.I)):
                out.append({'title': title, 'url': 'https://' + u0,
                            'disk': DISK_MAP.get(nd, nd),
                            'code': codes[0] if codes else '', 'size_mb': size_mb,
                            'share_time': time.strftime('%Y-%m-%d'),
                            'source': f'TG/{ch}', 'live': True})
    return out


def search_tg(kw, channels=None, limit_ch=20, timeout=12):
    chs = (channels or TG_CHANNELS_DEFAULT)[:limit_ch]
    res = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for f in as_completed([ex.submit(_tg_one, c, kw, timeout) for c in chs]):
            try:
                res += f.result()
            except Exception:
                pass
    return res, (None if res else f'TG 无结果（{len(chs)} 个频道）')


# ---------- 统一入口 ----------
_CJK = re.compile(r'[一-龥]+')
_ALNUM = re.compile(r'[a-zA-Z0-9]+')


# 常见别名：用户习惯的叫法 -> 网盘标题里实际常见的写法
# 只放能缩小范围的别名，避免把作者名单独放进来导致结果跑偏
ALIASES = {
    '肖四肖八': ['肖秀荣八套卷', '肖秀荣四套卷', '肖秀荣预测四套卷', '肖四pdf', '肖八pdf'],
    '肖四': ['肖秀荣四套卷', '肖秀荣预测四套卷', '肖四套卷', '肖四pdf'],
    '肖八': ['肖秀荣八套卷', '肖八套卷', '肖八pdf'],
    '肖1000': ['肖秀荣1000题', '肖1000题'],
    '腿姐四套卷': ['腿姐四套卷', '腿姐预测四套卷'],
    '徐涛八套卷': ['徐涛八套卷', '徐涛预测八套卷'],
}


def query_variants(q):
    """像人一样换关键词：生成一组可能命中的查询变体

    例: "2027年高考" -> ["2027年高考","2027","2027届","2027版","2027年","2027级"]
    例: "软考高项"   -> ["软考高项","软考","高项","软考高","考高项"]
    """
    q = (q or '').strip()
    if not q:
        return []
    out = [q]
    # 别名优先：pansou 是按标题字面匹配的，叫“肖四”但标题写“肖秀荣四套卷”就搜不到
    for k, al in ALIASES.items():
        if k in q:
            out.extend(al)
    nums = re.findall(r'\d{2,4}', q)
    cjk = _CJK.findall(q)
    alnum = _ALNUM.findall(q)

    if nums:
        for n in nums[:2]:
            out.append(n)
            for suf in ['届', '版', '年', '级']:
                out.append(n + suf)
        for n in nums[:1]:
            for c in cjk[:2]:
                if 2 <= len(c) <= 4:
                    out.append(n + c)
    for c in cjk:
        if 2 <= len(c) <= 6:
            out.append(c)
        if len(c) >= 4:
            for L in (2, 3):
                out.append(c[:L])
                out.append(c[-L:])
            if len(c) >= 6:
                for i in range(0, len(c) - 1, 2):
                    out.append(c[i:i + 2])
    for a in alnum:
        if len(a) >= 2 and not a.isdigit():
            out.append(a)

    seen, res = set(), []
    for t in out:
        t = t.strip()
        if t and t not in seen:
            seen.add(t)
            res.append(t)
    return res[:6]


def stream_search(kw, sources, selected, loose=True):
    """流式搜索：每完成一个 (源,关键词) 就 yield 一批结果

    yield: (source_key, variant, items, err)
    """
    if not kw:
        return
    jobs = []
    for key in selected:
        src = sources.get(key)
        if not src or not src.get('enabled'):
            continue
        k = src.get('kind')
        if k == 'pansou':
            qs = query_variants(kw)[:6] if loose else [kw]
            for q in qs:
                jobs.append((key, q, lambda u=src.get('url', ''), q=q: search_pansou(q, u, 25)))
        elif k == 'tg':
            qs = query_variants(kw)[:3] if loose else [kw]
            for q in qs:
                jobs.append((key, q, lambda s=src, q=q: search_tg(q, s.get('channels'), s.get('limit_ch', 20))))
        elif k == 'json':
            jobs.append((key, kw, lambda s=src: search_json(kw, s)))

    if not jobs:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        futs = {ex.submit(fn): (key, q) for key, q, fn in jobs}
        for f in as_completed(futs):
            key, q = futs[f]
            try:
                res, err = f.result()
            except Exception as e:
                res, err = [], str(e)[:50]
            yield key, q, res, err


def _relevant(title, kw):
    """相关性：用拆词后的片段判断，任一 2 字以上片段命中即算相关
    避免"高三一轮复习"整串匹配不到"2027年高考数学一轮复习"
    """
    if not kw or not title:
        return False
    t = title.lower()
    # 先试整串
    if kw.lower() in t:
        return True
    # 再用变体片段
    for term in query_variants(kw)[:6]:
        tl = term.lower()
        if len(tl) >= 2 and tl in t:
            return True
    return False


def run_search(kw, sources, selected, max_per=100, loose=False):
    """loose=True 时，对 pansou 类源用拆词多次请求再合并"""
    if not kw:
        return [], {}
    tasks = []
    for key in selected:
        s = sources.get(key)
        if not s or not s.get('enabled') or s.get('kind') == 'local':
            continue
        k = s.get('kind')
        if k == 'pansou':
            if loose:
                qs = query_variants(kw)[:6]
                def _multi(url=s.get('url', ''), qs=qs):
                    allout, errs, seen_u = [], [], set()
                    with ThreadPoolExecutor(max_workers=len(qs)) as ex:
                        futs = [ex.submit(search_pansou, q, url, 25) for q in qs]
                        for f in as_completed(futs):
                            try:
                                r, e = f.result()
                            except Exception as ex2:
                                errs.append(str(ex2)[:40]); continue
                            for it in r:
                                if it['url'] not in seen_u:
                                    seen_u.add(it['url'])
                                    allout.append(it)
                            if e: errs.append(e)
                    return allout, (None if allout else '; '.join(errs[:2]))
                tasks.append((key, _multi))
            else:
                tasks.append((key, lambda u=s.get('url', ''): search_pansou(kw, u)))
        elif k == 'tg':
            if loose:
                qs = query_variants(kw)[:3]
                def _tg_multi(s=s, qs=qs):
                    allout, seen_u = [], set()
                    with ThreadPoolExecutor(max_workers=len(qs)) as ex:
                        futs = [ex.submit(search_tg, q, s.get('channels'), s.get('limit_ch', 20)) for q in qs]
                        for f in as_completed(futs):
                            try:
                                r, _ = f.result()
                            except Exception:
                                continue
                            for it in r:
                                if it['url'] not in seen_u:
                                    seen_u.add(it['url'])
                                    allout.append(it)
                    return allout, (None if allout else 'TG 无结果')
                tasks.append((key, _tg_multi))
            else:
                tasks.append((key, lambda s=s: search_tg(kw, s.get('channels'), s.get('limit_ch', 20))))
        elif k == 'json':
            tasks.append((key, lambda s=s: search_json(kw, s)))
        else:
            # 自定义 http 源，按 json 处理
            tasks.append((key, lambda s=s: search_json(kw, s)))

    def work(t):
        key, fn = t
        t0 = time.time()
        try:
            res, err = fn()
        except Exception as e:
            res, err = [], str(e)[:70]
        return key, res[:max_per], err, int((time.time() - t0) * 1000)

    stats, items = {}, []
    if tasks:
        with ThreadPoolExecutor(max_workers=5) as ex:
            for key, res, err, ms in [f.result() for f in [ex.submit(work, t) for t in tasks]]:
                stats[key] = {'count': len(res), 'ms': ms, 'error': err}
                items += res

    seen, uniq, dropped = set(), [], 0
    for it in items:
        if it['url'] in seen:
            continue
        seen.add(it['url'])
        if not _relevant(it.get('title', ''), kw):
            dropped += 1
            continue
        uniq.append(it)
    if dropped:
        stats['_filtered'] = {'count': dropped, 'ms': 0, 'error': None}
    return uniq, stats


if __name__ == '__main__':
    import sys
    kw = sys.argv[1] if len(sys.argv) > 1 else '软考高项'
    src = load_sources()
    sel = [k for k, v in src.items() if v.get('enabled') and k != 'local']
    print('启用源:', sel)
    items, stats = run_search(kw, src, sel)
    print('统计:', json.dumps(stats, ensure_ascii=False))
    print(f'去重结果 {len(items)} 条:')
    for it in items[:12]:
        print(f"  [{it['disk']}] {it['title'][:46]} | {it['url'][:46]} | 码:{it['code'] or '-'}")
