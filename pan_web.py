# -*- coding: utf-8 -*-
"""pan_web.py — 网盘索引库可视化搜索（单文件，零第三方依赖）

启动:  python pan_web.py [--port 8899] [--host 0.0.0.0]
功能: 搜索 / 去重 / 体积筛选 / 时间筛选 / 自定义导出
"""
import argparse, json, sqlite3, os, sys, re, io, csv, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import probe_deep
import live_search
import search_sources as SS

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pan_index.db')

PREFIX_RE = re.compile(
    r'^(?:\[图片\]|🗣|📚|🆔|⭐️|✅|📺|🀄️|\s)*(?:学习资料|资源名称|资源标题|作品|名称|课程|标题|资源)?\s*[:：]\s*')
EMOJI_LEAD = re.compile(r'^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\s]+')
DESC_SPLIT = re.compile(r'\s*(?:📚\s*资料介绍|✨\s*作品亮点|作品亮点|资料介绍|资源简介|描述\s*[:：]|简介\s*[:：])')
BRACKET = re.compile(r'《([^》]{2,90})》')


def clean_title(t):
    if not t:
        return ''
    t = t.strip()
    for _ in range(3):
        t2 = EMOJI_LEAD.sub('', PREFIX_RE.sub('', t)).strip()
        if t2 == t:
            break
        t = t2
    m = BRACKET.search(t)
    if m:
        t = m.group(1).strip()
    else:
        t = DESC_SPLIT.split(t)[0].strip()
    return EMOJI_LEAD.sub('', t).strip(' 《》：:，,|｜-–')[:120]


def conn():
    c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    return c


def parse_filters(p):
    g = lambda k, d='': (p.get(k, [d])[0] or d)
    f = {
        'q': g('q').strip(),
        'disk': g('disk').strip(),
        'only_valid': g('only_valid', '0') == '1',
        'only_good': g('only_good', '0') == '1',
        'access': g('access'),
        'usable': g('usable', '0') == '1',
        'match': g('match', 'exact'),
        'hide_dead': g('hide_dead', '0') == '1',
        'disks': [x for x in (g('disks') or '').split(',') if x],
        'only_probed': g('only_probed', '0') == '1',
        'dedup': g('dedup', '1') == '1',
        'min_gb': g('min_gb'),
        'max_gb': g('max_gb'),
        'date_from': g('date_from'),
        'date_to': g('date_to'),
        'has_size': g('has_size', '0') == '1',
        'sort': g('sort', 'smart'),
    }
    return f


_CJK = re.compile(r'[\u4e00-\u9fa5]+')
_ALNUM = re.compile(r'[a-zA-Z0-9]+')


def split_terms(q):
    """把查询拆成用于 OR 匹配的词片段
    "2027高三" -> ["2027", "高三", "27高", "7高三"]  (中文 2 字滑窗)
    "尚硅谷 MySQL" -> ["尚硅谷", "硅谷", "MySQL"]
    """
    terms = []
    for m in _ALNUM.finditer(q):
        w = m.group(0)
        if len(w) >= 2:
            terms.append(w)
    for m in _CJK.finditer(q):
        seg = m.group(0)
        if len(seg) <= 4:
            terms.append(seg)
            # 长词再拆 2 字片段
            if len(seg) >= 3:
                for i in range(len(seg) - 1):
                    terms.append(seg[i:i + 2])
        else:
            # 超过 4 字：滑窗 2 字
            for i in range(len(seg) - 1):
                terms.append(seg[i:i + 2])
    # 去重保序
    out = []
    for t in terms:
        if t not in out:
            out.append(t)
    return out[:12]


def build_where(f, with_disk=True):
    where, params = ['1=1'], []
    mode = f.get('match', 'exact')
    if mode == 'loose' and f['q'].strip():
        terms = split_terms(f['q'])
        digits = [x for x in terms if x.isdigit()]
        words = [x for x in terms if not x.isdigit()]
        # 数字词（年份等）作为硬约束：必须命中，否则 2026 会挤掉 2027
        for d in digits:
            where.append('title LIKE ?')
            params.append('%' + d + '%')
        # 其余词 OR 匹配
        if words:
            ors = []
            for t in words:
                ors.append('title LIKE ?')
                params.append('%' + t + '%')
            where.append('(' + ' OR '.join(ors) + ')')
    else:
        for term in f['q'].split():
            where.append('title LIKE ?')
            params.append('%' + term + '%')
    if with_disk and f['disk']:
        where.append('disk = ?'); params.append(f['disk'])
    if f['disks']:
        where.append('disk IN (' + ','.join('?' * len(f['disks'])) + ')'); params.extend(f['disks'])
    if f['hide_dead']:
        where.append("(access <> 'dead' OR access IS NULL)")
    if f['only_valid']:
        where.append('valid = 1')
    if f['usable']:
        where.append("access IN ('accessible','with_code')")
    elif f['access']:
        where.append('access = ?'); params.append(f['access'])
    if f['only_probed']:
        where.append('probe IS NOT NULL')
    if f['only_good']:
        where.append("(probe LIKE '✅%' OR probe LIKE '📄%' OR probe LIKE '🟡%')")
    if f['dedup']:
        where.append('dup_of IS NULL')
    if f['has_size']:
        where.append('size_mb IS NOT NULL')
    try:
        if f['min_gb']:
            where.append('size_mb >= ?'); params.append(float(f['min_gb']) * 1024)
        if f['max_gb']:
            where.append('size_mb <= ?'); params.append(float(f['max_gb']) * 1024)
    except ValueError:
        pass
    if f['date_from']:
        where.append('share_time >= ?'); params.append(f['date_from'])
    if f['date_to']:
        where.append('share_time <= ?'); params.append(f['date_to'] + ' 99')
    return ' AND '.join(where), params


def score_title(title, q, mode='loose'):
    """Python 端相关性打分

    核心规则：查询里的数字词（年份/期数）是硬约束
      - 数字词未命中 → 分数大幅扣减（2026 不能冒充 2027）
      - 数字词命中   → 强加分
    其余词按命中给分，最后按命中率归一。
    """
    if not title or not q:
        return 0
    t = title.lower()
    ql = q.strip().lower()
    terms = split_terms(q)
    digit_terms = [x for x in terms if x.isdigit()]
    word_terms = [x for x in terms if not x.isdigit() and len(x) >= 2]

    score = 0
    if ql and ql in t:
        score += 200

    # 数字词：硬约束
    digit_hit = 0
    for d in digit_terms:
        if d in t:
            score += 120
            digit_hit += 1
    if digit_terms and digit_hit == 0:
        # 一个数字都没命中 → 直接判为不相关（返回极小值）
        return 1

    # 普通词
    word_hit = 0
    for w in word_terms:
        if w in t:
            score += 30
            word_hit += 1

    # 命中率加成
    total_terms = len(digit_terms) + len(word_terms)
    if total_terms:
        score += int(80 * (digit_hit + word_hit) / total_terms)
    return score


def relevance_expr(q, mode):
    """loose 模式 SQL 粗排：数字词加权，保证 2027 优先于 2026 被选中"""
    if mode != 'loose' or not q.strip():
        return ''
    qq = q.strip().replace("'", "''")
    terms = split_terms(q)
    score = ["(CASE WHEN title LIKE '%" + qq + "%' THEN 100 ELSE 0 END)"]
    for t in terms:
        if t.isdigit():
            score.append("(CASE WHEN title LIKE '%" + t + "%' THEN 50 ELSE 0 END)")
    for t in terms:
        if not t.isdigit() and len(t) >= 2:
            score.append("(CASE WHEN title LIKE '%" + t.replace("'", "''") + "%' THEN 5 ELSE 0 END)")
    return '(' + ' + '.join(score) + ') DESC, '
ORDERS = {
    'smart': ("CASE WHEN probe LIKE '✅%' THEN 0 WHEN probe LIKE '🟡%' THEN 1 WHEN probe IS NULL THEN 2 ELSE 3 END, "
              "CASE WHEN valid=1 THEN 0 WHEN valid IS NULL THEN 1 ELSE 2 END, COALESCE(size_mb,0) DESC"),
    'recent': 'share_time DESC, first_seen DESC',
    'size_desc': 'COALESCE(size_mb,-1) DESC',
    'size_asc': 'COALESCE(size_mb,999999999) ASC',
    'title': 'title ASC',
    'disk': 'disk ASC, COALESCE(size_mb,0) DESC',
}


def do_search(p):
    f = parse_filters(p)
    use_live = (p.get('live', ['0'])[0] == '1')
    skip_local = (p.get('skip_local', ['0'])[0] == '1')
    live_srcs = [x for x in (p.get('live_sources', [''])[0] or '').split(',') if x]
    try:
        page = max(1, int(p.get('page', ['1'])[0]))
        size = min(200, max(10, int(p.get('size', ['30'])[0])))
    except ValueError:
        page, size = 1, 30
    where, params = build_where(f)
    order = relevance_expr(f['q'], f['match']) + ORDERS.get(f['sort'], ORDERS['smart'])
    c = conn()
    total = c.execute(f'SELECT COUNT(*) FROM items WHERE {where}', params).fetchone()[0]
    # facets: 忽略 disk 过滤，统计当前关键词命中的网盘分布
    bwhere, bparams = build_where(f, with_disk=False)
    facets = {r[0]: r[1] for r in c.execute(
        f'SELECT disk, COUNT(*) FROM items WHERE {bwhere} GROUP BY disk', bparams).fetchall()}
    # loose 模式：多取候选（最多 600 条），Python 精排后再分页
    fetch_size = 2000 if (f.get('match') == 'loose' and f['q'].strip()) else size
    fetch_off = 0 if fetch_size > size else (page - 1) * size
    rows = c.execute(
        f'SELECT url,title,disk,code,size,size_mb,share_time,valid,probe,needs_code,source,first_seen,'
        f'dup_of,title_key,access FROM items WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?',
        params + [fetch_size, fetch_off]).fetchall()
    # 每组重复数
    keys = [r['title_key'] for r in rows if r['title_key']]
    dupcount = {}
    if keys:
        qs = ','.join('?' * len(keys))
        for k, n in c.execute(f'SELECT title_key, COUNT(*) FROM items WHERE title_key IN ({qs}) GROUP BY title_key', keys):
            dupcount[k] = n
    saved_urls = set()
    saved_rows = []
    try:
        saved_urls = {r[0] for r in c.execute('SELECT url FROM saved')}
        # 资源库里命中关键词的也并进来（saved 是"我的"，优先级更高）
        sq, sp = ['1=1'], []
        if f.get('match') == 'loose' and f['q'].strip():
            terms = split_terms(f['q'])
            if terms:
                ors = []
                for t in terms:
                    ors.append('(title LIKE ? OR note LIKE ? OR tags LIKE ?)')
                    sp += ['%' + t + '%'] * 3
                sq.append('(' + ' OR '.join(ors) + ')')
        else:
            for term in f['q'].split():
                sq.append('(title LIKE ? OR note LIKE ? OR tags LIKE ?)')
                sp += ['%' + term + '%'] * 3
        if f['disk']:
            sq.append('disk = ?'); sp.append(f['disk'])
        if f['access']:
            sq.append('access = ?'); sp.append(f['access'])
        saved_rows = c.execute(
            f'SELECT url,title,disk,code,size_mb,share_time,valid,probe,access,source,tags,saved_at,note '
            f'FROM saved WHERE {" AND ".join(sq)} ORDER BY saved_at DESC LIMIT 200', sp).fetchall()
    except Exception:
        pass
    c.close()
    out, seen_out = [], set()
    for r in saved_rows:
        d = dict(r)
        d['raw_title'] = d['title']
        d['title'] = clean_title(d['title'])
        d['size'] = ''
        d['valid'] = 1 if d.get('access') in ('accessible', 'with_code') else d.get('valid')
        d['dups'] = 1
        d['live'] = False
        d['saved'] = True
        seen_out.add(d['url'])
        out.append(d)
    for r in rows:
        if r['url'] in seen_out:
            continue
        d = dict(r)
        d['raw_title'] = d['title']
        d['title'] = clean_title(d['title'])
        d['dups'] = dupcount.get(d['title_key'], 1)
        d['live'] = False
        d['saved'] = r['url'] in saved_urls
        d['tags'] = ''
        out.append(d)

    live_stats = {}
    if False and use_live and f['q'] and page == 1:  # 实时搜索改为独立接口
        srcs = SS.load_sources()
        items_live, live_stats = SS.run_search(f['q'], srcs, live_srcs or ['pansou', 'tg'])
        # 已有 URL 集合，避免重复
        exist = {d['url'] for d in out}
        items_live.sort(key=lambda x: -score_title(x.get('title', ''), f['q'], 'loose'))
        for it in items_live:
            if it['url'] in exist:
                continue
            exist.add(it['url'])
            d = {
                'url': it['url'], 'title': clean_title(it['title']), 'raw_title': it['title'],
                'disk': it['disk'], 'code': it.get('code', ''), 'size': '', 'size_mb': it.get('size_mb'),
                'share_time': it.get('share_time', ''), 'valid': None, 'probe': None,
                'needs_code': '', 'source': it.get('source', ''), 'first_seen': '',
                'dup_of': None, 'title_key': '', 'access': 'unknown', 'dups': 1, 'live': True,
            }
            out.append(d)
        total = len(out) if page == 1 else total


    # Python 端精排（SQL 的 LIKE 打分不够准）
    # 标题归一化去重（本地结果也去重，避免同课多分享）
    if f.get('dedup'):
        seen_tk, uniq = set(), []
        for x in out:
            tk = _norm_title(x.get('title', ''))
            if tk and tk in seen_tk:
                continue
            if tk:
                seen_tk.add(tk)
            uniq.append(x)
        out = uniq

    if f.get('match') == 'loose' and f['q'].strip():
        out.sort(key=lambda x: -score_title(x.get('title', ''), f['q'], 'loose'))
        # 精排后手动分页
        start = (page - 1) * size
        out = out[start:start + size]
    if skip_local:
        out = []
        total = 0
    if page == 1:
        total = max(total, len(out))
    return {'total': total, 'page': page, 'size': size, 'items': out,
            'live_stats': live_stats, 'facets': facets}


import unicodedata


def _url_key(u):
    """链接归一化：只保留盘符+分享ID，忽略 http/https、?pwd=、# 片段
    以及同盘的链接写法差异（百度 /share/init?surl=、阿里云盘新旧域名）"""
    u = (u or '').strip()
    try:
        s = urllib.parse.urlsplit(u)
        if not s.netloc:
            return u.split('?')[0].split('#')[0].rstrip('/')
        host = s.netloc.lower()
        path = (s.path or '').rstrip('/')
        if host.endswith('aliyundrive.com'):
            host = 'www.alipan.com'
        if host.endswith('pan.baidu.com'):
            surl = (urllib.parse.parse_qs(s.query).get('surl') or [''])[0]
            if surl:
                path = '/s/' + (surl[1:] if surl.startswith('1') else surl)
            if path.startswith('/s/'):
                sid = path[3:]
                if sid.startswith('1'):
                    sid = sid[1:]
                return 'pan.baidu.com/s/' + sid
        return host + path
    except ValueError:
        return u.split('?')[0].split('#')[0].rstrip('/')


def _saved_url_keys():
    c = sqlite3.connect(DB, timeout=30)
    try:
        return {_url_key(r[0]) for r in c.execute('SELECT url FROM saved')}
    finally:
        c.close()


def _norm_title(t):
    """标题归一化：去掉标点/空白/序号后缀，用于判重"""
    if not t:
        return ''
    out = []
    for ch in t:
        if ch.isspace():
            continue
        cat = unicodedata.category(ch)
        if cat.startswith('P') or cat.startswith('S'):
            continue
        out.append(ch)
    s = ''.join(out)
    for _ in range(2):
        s2 = _SUFFIX.sub('', s)
        if s2 == s:
            break
        s = s2
    return s.lower()[:70]


_SUFFIX = re.compile(r'(\(?\d+\)?|（\d+）|副本|copy)$', re.I)


def do_saved_list(p):
    q = (p.get('q', [''])[0] or '').strip()
    disk = (p.get('disk', [''])[0] or '').strip()
    tag = (p.get('tag', [''])[0] or '').strip()
    sort = (p.get('sort', ['recent'])[0] or 'recent')
    try:
        page = max(1, int(p.get('page', ['1'])[0])); size = min(100, max(10, int(p.get('size', ['30'])[0])))
    except ValueError:
        page, size = 1, 30
    where, params = ['1=1'], []
    for term in q.split():
        where.append('(title LIKE ? OR note LIKE ? OR tags LIKE ?)')
        params += ['%' + term + '%'] * 3
    if disk:
        where.append('disk = ?'); params.append(disk)
    if tag:
        where.append('tags = ?'); params.append(tag)
    only_dead = (p.get('only_dead', ['0'])[0] == '1')
    if only_dead:
        where.append("access='dead'")
    dedup = (p.get('dedup', ['1'])[0] or '1') == '1'
    order = {'recent': 'saved_at DESC', 'title': 'title ASC', 'size_desc': 'COALESCE(size_mb,-1) DESC'}.get(sort, 'saved_at DESC')
    c = conn()
    total = c.execute(f'SELECT COUNT(*) FROM saved WHERE {" AND ".join(where)}', params).fetchone()[0]
    rows = c.execute(f'SELECT * FROM saved WHERE {" AND ".join(where)} ORDER BY {order} LIMIT ? OFFSET ?',
                     params + [size, (page - 1) * size]).fetchall()
    all_rows = c.execute(f'SELECT * FROM saved WHERE {" AND ".join(where)} ORDER BY {order}', params).fetchall()
    c.close()
    items, seen = [], {}
    for r in all_rows:
        d = dict(r)
        d['raw_title'] = d['title']
        d['title'] = clean_title(d['title'])
        if dedup:
            k = _norm_title(d['title'])
            if k and k in seen:
                seen[k]['dups'] = seen[k].get('dups', 1) + 1
                continue
            seen[k] = d
            d['dups'] = 1
        else:
            d['dups'] = 1
        items.append(d)
    total = len(items)
    start = (page - 1) * size
    return {'total': total, 'page': page, 'size': size, 'items': items[start:start + size]}


def do_dead_list(p):
    """失效资源清单（用于提示用户补链）"""
    try:
        limit = min(200, max(1, int(p.get('limit', ['50'])[0] or 50)))
    except ValueError:
        limit = 50
    c = conn()
    rows = c.execute("SELECT url,title,disk,code,access,saved_at,tags FROM saved "
                     "WHERE access='dead' ORDER BY saved_at DESC LIMIT ?", (limit,)).fetchall()
    total = c.execute("SELECT COUNT(*) FROM saved WHERE access='dead'").fetchone()[0]
    c.close()
    items = []
    for r in rows:
        d = dict(r)
        d['title'] = clean_title(d['title'])
        items.append(d)
    return {'total': total, 'items': items}


def do_saved_stats():
    _ensure_tag_table()
    c = conn()
    tags = [{'name': r[0], 'n': r[1]} for r in c.execute(
        "SELECT tags, COUNT(*) FROM saved WHERE COALESCE(tags,'')<>'' GROUP BY tags ORDER BY 2 DESC").fetchall()]
    known = {t['name'] for t in tags}
    for r in c.execute('SELECT name FROM tag_defs ORDER BY created_at'):
        if r[0] and r[0] not in known:
            tags.append({'name': r[0], 'n': 0})
    s = {'total': c.execute('SELECT COUNT(*) FROM saved').fetchone()[0],
         'disks': [{'name': r[0], 'n': r[1]} for r in c.execute(
             'SELECT disk, COUNT(*) FROM saved GROUP BY disk ORDER BY 2 DESC').fetchall()],
         'tags': tags,
         'dead': c.execute("SELECT COUNT(*) FROM saved WHERE access='dead'").fetchone()[0]}
    c.close()
    return s


def _ensure_tag_table():
    """分类定义表：新建的分类即使还没挂资源也保留"""
    c = sqlite3.connect(DB, timeout=15)
    try:
        c.execute('CREATE TABLE IF NOT EXISTS tag_defs(name TEXT PRIMARY KEY, created_at TEXT)')
        c.commit()
    finally:
        c.close()


def do_tag_create(p):
    name = (p.get('name', [''])[0] or '').strip()[:40]
    if not name:
        return {'ok': False, 'error': 'name required'}
    _ensure_tag_table()
    c = sqlite3.connect(DB, timeout=15)
    try:
        c.execute('INSERT OR IGNORE INTO tag_defs(name,created_at) VALUES(?,?)',
                  (name, __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')))
        c.commit()
    finally:
        c.close()
    return {'ok': True, 'name': name}


def do_save(p):
    url = (p.get('url', [''])[0] or '').strip()
    if not url:
        return {'ok': False, 'error': 'url required'}
    title = (p.get('title', [''])[0] or '')[:300]
    disk = (p.get('disk', [''])[0] or '')
    code = (p.get('code', [''])[0] or '')
    note = (p.get('note', [''])[0] or '')
    tags = (p.get('tags', [''])[0] or '').strip() or '未分类'
    size_mb = (p.get('size_mb', [''])[0] or '')
    share_time = (p.get('share_time', [''])[0] or '')
    source = (p.get('source', [''])[0] or '')
    now = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')
    c = sqlite3.connect(DB, timeout=30)
    try:
        # 同标题的旧分享先删掉（避免重复堆积）
        nk = _norm_title(title)
        if nk:
            for old_url, old_title in c.execute('SELECT url, title FROM saved').fetchall():
                if old_url != url and _norm_title(old_title) == nk:
                    c.execute('DELETE FROM saved WHERE url=?', (old_url,))
        c.execute('''INSERT INTO saved(url,title,disk,code,size_mb,share_time,source,note,tags,saved_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(url) DO UPDATE SET title=excluded.title, code=excluded.code,
              note=excluded.note, tags=excluded.tags''',
            (url, title, disk, code, int(size_mb) if str(size_mb).isdigit() else None,
             share_time, source, note, tags, now))
        if tags:
            c.execute('CREATE TABLE IF NOT EXISTS tag_defs(name TEXT PRIMARY KEY, created_at TEXT)')
            c.execute('INSERT OR IGNORE INTO tag_defs(name,created_at) VALUES(?,?)', (tags, now))
        c.commit()
    finally:
        c.close()
    return {'ok': True}


def do_unsave(p):
    url = (p.get('url', [''])[0] or '').strip()
    c = sqlite3.connect(DB, timeout=30)
    try:
        c.execute('DELETE FROM saved WHERE url=?', (url,)); c.commit()
    finally:
        c.close()
    return {'ok': True}


def _save_live_to_items(items):
    """实时搜索结果写入 items 表（已存在则跳过），让下次搜索秒出"""
    if not items:
        return 0
    import datetime
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    c = sqlite3.connect(DB, timeout=15)
    n = 0
    try:
        for it in items:
            url = it.get('url')
            if not url:
                continue
            if c.execute('SELECT 1 FROM items WHERE url=?', (url,)).fetchone():
                continue
            title = (it.get('raw_title') or it.get('title') or '')[:200]
            c.execute('''INSERT OR IGNORE INTO items
                (url,title,disk,code,size,kw,source,first_seen,access,needs_code)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (url, title, it.get('disk', ''), it.get('code', ''), '', '实时',
                 it.get('source', 'live'), now, 'unknown', ''))
            n += 1
        c.commit()
    finally:
        c.close()
    return n


def do_stats():
    c = conn()
    s = {
        'total': c.execute('SELECT COUNT(*) FROM items').fetchone()[0],
        'unique': c.execute('SELECT COUNT(*) FROM items WHERE dup_of IS NULL').fetchone()[0],
        'valid': c.execute('SELECT COUNT(*) FROM items WHERE valid=1').fetchone()[0],
        'usable': c.execute("SELECT COUNT(*) FROM items WHERE access IN ('accessible','with_code')").fetchone()[0],
        'coded': c.execute("SELECT COUNT(*) FROM items WHERE COALESCE(code,'')<>''").fetchone()[0],
        'probed': c.execute('SELECT COUNT(*) FROM items WHERE probe IS NOT NULL').fetchone()[0],
        'good': c.execute("SELECT COUNT(*) FROM items WHERE probe LIKE '✅%' OR probe LIKE '🟡%' OR probe LIKE '📄%'").fetchone()[0],
        'sized': c.execute('SELECT COUNT(*) FROM items WHERE size_mb IS NOT NULL').fetchone()[0],
        'disks': [{'name': r[0], 'n': r[1]} for r in c.execute(
            'SELECT disk, COUNT(*) FROM items WHERE dup_of IS NULL GROUP BY disk ORDER BY 2 DESC').fetchall()],
        'access': {r[0]: r[1] for r in c.execute(
            'SELECT access, COUNT(*) FROM items WHERE dup_of IS NULL GROUP BY access').fetchall()},
    }
    c.close()
    return s


EXPORT_FIELDS = {
    'name': '资源名称', 'url': '网盘链接', 'code': '提取码', 'disk': '网盘',
    'share_time': '分享时间', 'size': '文件大小', 'probe': '内容体检',
    'valid': '有效性', 'source': '来源', 'first_seen': '收录时间',
}
PRESETS = {
    'simple': ['name', 'disk', 'url', 'code'],
    'standard': ['name', 'disk', 'url', 'code', 'size', 'share_time'],
    'full': ['name', 'disk', 'url', 'code', 'size', 'share_time', 'probe', 'valid', 'source', 'first_seen'],
}


def do_export(p):
    f = parse_filters(p)
    fields = [x for x in (p.get('fields', [''])[0] or '').split(',') if x in EXPORT_FIELDS]
    if not fields:
        fields = PRESETS['standard']
    fmt = (p.get('fmt', ['tsv'])[0] or 'tsv').lower()
    where, params = build_where(f)
    order = relevance_expr(f['q'], f['match']) + ORDERS.get(f['sort'], ORDERS['smart'])
    c = conn()
    rows = c.execute(
        f'SELECT url,title,disk,code,size,size_mb,share_time,valid,probe,source,first_seen,needs_code '
        f'FROM items WHERE {where} ORDER BY {order} LIMIT 20000', params).fetchall()
    c.close()

    def fmt_size(r):
        if r['size_mb']:
            mb = r['size_mb']
            return f'{mb/1024:.2f}GB' if mb >= 1024 else f'{mb}MB'
        return r['size'] or ''

    def fmt_valid(v):
        return '有效' if v == 1 else ('失效' if v == 0 else '未验')

    out_rows = []
    for r in rows:
        rec = {}
        for fld in fields:
            if fld == 'name':
                rec[fld] = clean_title(r['title'])
            elif fld == 'url':
                rec[fld] = r['url']
            elif fld == 'code':
                rec[fld] = r['code'] or ('无需' if r['needs_code'] == 'no' else '')
            elif fld == 'disk':
                rec[fld] = r['disk'] or ''
            elif fld == 'share_time':
                rec[fld] = (r['share_time'] or '')[:10]
            elif fld == 'size':
                rec[fld] = fmt_size(r)
            elif fld == 'probe':
                rec[fld] = r['probe'] or ''
            elif fld == 'valid':
                rec[fld] = fmt_valid(r['valid'])
            elif fld == 'source':
                rec[fld] = r['source'] or ''
            elif fld == 'first_seen':
                rec[fld] = (r['first_seen'] or '')[:16]
        out_rows.append(rec)

    header = [EXPORT_FIELDS[x] for x in fields]
    buf = io.StringIO()
    if fmt == 'csv':
        w = csv.writer(buf, lineterminator='\n')
        w.writerow(header)
        for rec in out_rows:
            w.writerow([rec.get(x, '') for x in fields])
        data = '\ufeff' + buf.getvalue()   # BOM 让 Excel 正确识别 UTF-8
        ctype = 'text/csv; charset=utf-8'
    elif fmt == 'md':
        buf.write('| ' + ' | '.join(header) + ' |\n')
        buf.write('|' + '|'.join(['---'] * len(header)) + '|\n')
        for rec in out_rows:
            buf.write('| ' + ' | '.join(str(rec.get(x, '')).replace('|', '\\|') for x in fields) + ' |\n')
        data = buf.getvalue()
        ctype = 'text/markdown; charset=utf-8'
    else:  # tsv
        buf.write('\t'.join(header) + '\n')
        for rec in out_rows:
            buf.write('\t'.join(str(rec.get(x, '')).replace('\t', ' ') for x in fields) + '\n')
        data = '\ufeff' + buf.getvalue()
        ctype = 'text/tab-separated-values; charset=utf-8'
    return data, ctype, len(out_rows)


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>网盘索引库</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --paper:#F5F0EB;        /* 暖米白纸面 */
  --card:#FFFDFB;
  --ink:#2D3436;          /* 近黑墨 */
  --ink-2:#5A6062;
  --ink-3:#8A8F90;
  --line:#E5DED5;
  --line-2:#EFE9E1;
  --sage:#6B8F71;         /* 墨绿主色 */
  --sage-deep:#4E6B54;
  --sage-soft:#E8EEE6;
  --clay:#B8815F;         /* 陶土色 强调 */
  --clay-soft:#F3E8DF;
  --ok:#4E7A57;--ok-soft:#E8EFE7;
  --warn:#9A6B2F;--warn-soft:#F6EBDC;
  --bad:#A4504A;--bad-soft:#F6E4E2;
  --serif:'Songti SC','STSong','SimSun',Georgia,serif;
  --sans:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
}
html{scroll-behavior:smooth}
body{background:var(--paper);color:var(--ink);font-family:var(--sans);
font-size:14px;line-height:1.65;-webkit-font-smoothing:antialiased}

/* ============ 顶部固定栏 ============ */
.top{position:fixed;top:0;left:0;right:0;z-index:60;
background:rgba(245,240,235,.94);backdrop-filter:blur(16px) saturate(120%);
border-bottom:1px solid var(--line)}
.top-in{max-width:1180px;margin:0 auto;padding:0 40px}
.bar{display:flex;align-items:center;gap:24px;height:58px}
.logo{display:flex;align-items:center;gap:11px;font-family:var(--serif);
font-size:19px;font-weight:500;letter-spacing:-.2px;white-space:nowrap}
.logo-mark{width:32px;height:32px;border-radius:50%;background:var(--sage);
display:flex;align-items:center;justify-content:center;flex:0 0 32px}
.logo-mark svg{width:16px;height:16px;color:var(--paper)}
.tabs{display:flex;gap:4px;background:var(--card);border:1px solid var(--line-2);
border-radius:100px;padding:4px}
.tab{background:transparent;border:0;padding:7px 18px;border-radius:100px;
font-family:var(--sans);font-size:13px;color:var(--ink-3);cursor:pointer;transition:.16s}
.tab:hover{color:var(--ink)}
.tab.on{background:var(--ink);color:var(--paper);font-weight:500}
.tab .cnt2{font-size:11px;opacity:.7;margin-left:3px}
.metrics{display:flex;gap:34px;margin-left:auto}
.metric{text-align:right;line-height:1.2}
.metric .n{display:block;font-size:19px;font-weight:600;letter-spacing:-.4px;
font-variant-numeric:tabular-nums;color:var(--ink)}
.metric .l{font-size:11.5px;color:var(--ink-3);letter-spacing:.3px}
.metric.hi .n{color:var(--sage-deep)}

/* 搜索行 */
.search-row{display:flex;gap:9px;padding-bottom:11px}
.field{flex:1;position:relative;min-width:0}
.field svg{position:absolute;left:18px;top:50%;transform:translateY(-50%);
width:17px;height:17px;color:var(--ink-3);pointer-events:none}
input[type=text].q{width:100%;height:42px;padding:0 18px 0 46px;
background:var(--card);border:1px solid var(--line);border-radius:100px;
font-family:var(--sans);font-size:14.5px;color:var(--ink);outline:none;
transition:.2s}
input[type=text].q::placeholder{color:#B0B4B3}
input[type=text].q:focus{border-color:var(--sage);box-shadow:0 0 0 3px rgba(107,143,113,.1)}
.pill{height:42px;padding:0 22px;border-radius:100px;border:1px solid transparent;
font-family:var(--sans);font-size:13.5px;font-weight:500;cursor:pointer;
display:inline-flex;align-items:center;gap:8px;transition:.2s;white-space:nowrap}
.pill svg{width:15px;height:15px}
.pill-solid{background:var(--ink);color:var(--paper)}
.pill-solid:hover{background:#3D4547}
.pill-line{background:transparent;color:var(--ink-2);border-color:var(--line)}
.pill-line:hover{border-color:#CFC6BA;color:var(--ink);background:var(--card)}
.pill-live{background:var(--sage);color:#fff;border-color:var(--sage)}
.pill-live:hover{background:var(--sage-deep)}
.pill-ghost{background:transparent;color:var(--ink-3);border-color:var(--line)}
.pill-ghost:hover{border-color:var(--sage);color:var(--sage-deep)}

/* 筛选行 */
.filters{display:flex;gap:7px;align-items:center;flex-wrap:wrap;padding-bottom:12px}
.filterbar{position:sticky;z-index:39;background:var(--paper);
padding:10px 0 12px;margin:-6px 0 8px;display:flex;gap:7px;flex-wrap:wrap;align-items:center;
border-bottom:1px solid var(--line-2)}
.fgroup{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap}
.flabel{font-size:11px;color:var(--ink-3);letter-spacing:.5px;margin-right:2px}
.chip{padding:6px 13px;border-radius:100px;border:1px solid var(--line);
background:transparent;color:var(--ink-2);font-size:12.5px;cursor:pointer;
transition:.18s;white-space:nowrap;display:inline-flex;align-items:center;gap:6px}
.chip:hover{border-color:#CFC6BA;color:var(--ink)}
.chip.on{background:var(--ink);border-color:var(--ink);color:var(--paper)}
.chip.bad{color:var(--bad);border-color:var(--bad-soft)}
.chip.bad.on{background:var(--bad);border-color:var(--bad);color:#fff}
.chip.bad .cnt{color:var(--bad)}
.chip.on .cnt{color:rgba(245,240,235,.6)}
.chip.only{background:var(--sage-soft);border-color:#C9D8C6;color:var(--sage-deep);font-weight:500}
.chip.only.on{background:var(--sage);border-color:var(--sage);color:#fff}
.cnt{font-size:11.5px;color:var(--ink-3);font-variant-numeric:tabular-nums}
.divider{width:1px;height:18px;background:var(--line);margin:0 4px}
.range{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;color:var(--ink-3)}
.range input{height:32px;background:var(--card);border:1px solid var(--line);
border-radius:100px;padding:0 13px;font-family:var(--sans);font-size:12.5px;
color:var(--ink);outline:none;width:72px}
.range input[type=date]{width:130px;padding:0 12px 0 14px}
.range input:focus{border-color:var(--sage)}
select.sort{height:32px;background:var(--card);border:1px solid var(--line);
border-radius:100px;padding:0 30px 0 14px;font-family:var(--sans);font-size:12.5px;
color:var(--ink-2);outline:none;cursor:pointer;appearance:none;
background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%238A8F90' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
background-repeat:no-repeat;background-position:right 10px center;background-size:13px}
select.sort:focus{border-color:var(--sage)}

/* ============ 主区 ============ */
main{max-width:1180px;margin:0 auto;padding:0 40px 60px}
.summary{position:sticky;z-index:40;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
padding:12px 0 11px;border-bottom:1px solid var(--line-2);margin-bottom:16px;
background:var(--paper);top:170px}
.summary::before{content:'';position:absolute;left:-40px;right:-40px;top:0;bottom:0;
background:var(--paper);z-index:-1}
.summary .big{font-family:var(--serif);font-size:21px;font-weight:500;
letter-spacing:-.5px;color:var(--ink)}
.summary .big b{color:var(--sage-deep);font-weight:500}
.summary .tags{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-left:auto}
.tag{font-size:11.5px;color:var(--ink-3);background:var(--card);
border:1px solid var(--line);padding:4px 11px;border-radius:100px}

/* ============ 结果卡片 ============ */
.item{background:var(--card);border:1px solid var(--line-2);border-radius:13px;
padding:15px 20px;margin-bottom:9px;transition:.18s;position:relative}
.item:hover{border-color:#D8CFC3;box-shadow:0 2px 14px rgba(45,52,54,.045)}
.item.gone{opacity:.5}
.item.gone:hover{opacity:.8}
.t{font-size:14.5px;font-weight:500;line-height:1.45;letter-spacing:-.1px;
margin-bottom:9px;color:var(--ink);cursor:default;word-break:break-word;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:9px}
.b{font-size:11px;padding:2.5px 8px;border-radius:6px;background:#F2EEE8;
color:var(--ink-2);white-space:nowrap;display:inline-flex;align-items:center;gap:4px;
font-variant-numeric:tabular-nums}
.b.s-ok{background:var(--ok-soft);color:var(--ok)}
.b.s-sage{background:var(--sage-soft);color:var(--sage-deep)}
.b.s-warn{background:var(--warn-soft);color:var(--warn)}
.b.s-bad{background:var(--bad-soft);color:var(--bad)}
.b.s-clay{background:var(--clay-soft);color:var(--clay)}
.link-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.url{flex:1;min-width:240px;font-family:ui-monospace,'SF Mono',Consolas,monospace;
font-size:12px;color:var(--sage-deep);text-decoration:none;word-break:break-all;
letter-spacing:-.2px}
.url:hover{text-decoration:underline;text-underline-offset:3px}
.mini{height:29px;padding:0 13px;font-size:11.5px;border-radius:100px;background:transparent;
border:1px solid var(--line);color:var(--ink-3);font-family:var(--sans);
font-size:12px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;
transition:.18s;white-space:nowrap}
.mini:hover{border-color:var(--sage);color:var(--sage-deep);background:var(--sage-soft)}
.mini.saved{background:var(--sage-soft);border-color:#C9D8C6;color:var(--sage-deep)}
.mini.del:hover{border-color:var(--bad);color:var(--bad);background:var(--bad-soft)}
.mini svg{width:13px;height:13px}
.source{margin-top:7px;font-size:11px;color:#A8ACAB;letter-spacing:.1px}

/* 分页 */
.pages{display:flex;gap:10px;justify-content:center;align-items:center;
margin-top:40px}
.pages .now{font-size:13px;color:var(--ink-3);padding:0 8px;font-variant-numeric:tabular-nums}
.pg{height:38px;padding:0 20px;border-radius:100px;border:1px solid var(--line);
background:var(--card);color:var(--ink-2);font-family:var(--sans);font-size:13px;
cursor:pointer;transition:.18s}
.pg:hover{border-color:var(--sage);color:var(--sage-deep)}

/* 空态 */
.livebar{background:var(--sage-soft);border:1px dashed #C9D8C6;border-radius:11px;
padding:10px 16px;margin-bottom:10px;font-size:12.5px;color:var(--sage-deep)}
.livebar b{font-weight:600}
.spin{display:inline-block;width:11px;height:11px;border:2px solid #C9D8C6;
border-top-color:var(--sage);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-1px}
@keyframes sp{to{transform:rotate(360deg)}}
.deadtip{position:fixed;left:24px;bottom:24px;z-index:80;max-width:340px;
background:var(--card);border:1px solid var(--bad-soft);border-left:3px solid var(--bad);
border-radius:12px;padding:14px 16px;box-shadow:0 12px 32px rgba(45,52,54,.14);
cursor:pointer;animation:tin .35s ease}
@keyframes tin{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
.dt-head{display:flex;justify-content:space-between;align-items:center;
font-size:13px;font-weight:600;color:var(--bad);margin-bottom:8px}
.dt-close{color:var(--ink-3);font-weight:400;font-size:12px}
.dt-example{font-size:12.5px;color:var(--ink-2);line-height:1.55;margin-bottom:6px;
word-break:break-all}
.dt-pick{display:block;padding:6px 8px;margin:4px -8px;border-radius:6px;transition:.15s}
.dt-pick:hover{background:var(--bad-soft)}
.dt-hint{font-size:11.5px;color:var(--ink-3)}
.blank{text-align:center;padding:110px 20px}
.blank svg{width:52px;height:52px;color:#D3CCC3;margin-bottom:22px}
.blank .h{font-family:var(--serif);font-size:20px;color:var(--ink-2);margin-bottom:9px;font-weight:400}
.blank .p{font-size:13px;color:var(--ink-3)}

/* ============ 弹窗 ============ */
.veil{position:fixed;inset:0;background:rgba(45,52,54,.42);backdrop-filter:blur(3px);
display:none;align-items:center;justify-content:center;z-index:100;padding:24px}
.veil.show{display:flex}
.sheet{background:var(--paper);border-radius:22px;padding:38px 40px;width:100%;
max-width:600px;max-height:86vh;overflow:auto;
box-shadow:0 30px 80px rgba(45,52,54,.22);border:1px solid var(--line)}
.sheet h2{font-family:var(--serif);font-size:23px;font-weight:500;
letter-spacing:-.3px;margin-bottom:26px;color:var(--ink)}
.grp{margin-bottom:24px}
.grp>.lab{font-size:12px;font-weight:500;color:var(--ink-3);
letter-spacing:.5px;margin-bottom:11px;text-transform:uppercase}
.sheet input[type=text],.sheet select{width:100%;height:44px;background:var(--card);
border:1px solid var(--line);border-radius:12px;padding:0 15px;font-family:var(--sans);
font-size:14px;color:var(--ink);outline:none}
.sheet input[type=text]:focus,.sheet select:focus{border-color:var(--sage)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:11px}
.tick{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--ink-2);
cursor:pointer;padding:11px 14px;border-radius:11px;background:var(--card);
border:1px solid transparent;transition:.16s}
.tick:hover{border-color:var(--line)}
.tick.on{border-color:var(--sage);background:var(--sage-soft);color:var(--sage-deep);font-weight:500}
.tick input{accent-color:var(--sage);width:15px;height:15px;cursor:pointer}
.note{font-size:12px;color:var(--ink-3);margin-top:9px;line-height:1.6}
.srcrow{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:11px 14px;
border-radius:11px;background:var(--card);border:1px solid var(--line-2);transition:.16s}
.srcrow.on{border-color:var(--sage);background:var(--sage-soft)}
.srcrow .srcd{width:100%;font-size:11.5px;color:var(--ink-3);padding-left:25px}
.srcrow.on .srcd{color:var(--sage-deep)}
.sheet-foot{display:flex;gap:11px;margin-top:30px}
.sheet-foot .pill{flex:1;justify-content:center;height:44px}

/* 目录树 */
.tree{font-family:ui-monospace,'SF Mono',Consolas,monospace;font-size:12.5px;
line-height:1.9;background:var(--card);border:1px solid var(--line);
border-radius:14px;padding:18px 20px;max-height:48vh;overflow:auto}
.node{display:flex;align-items:center;gap:7px;padding:1.5px 0}
.node svg{width:13px;height:13px;flex:0 0 13px}
.node .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.node .m{color:var(--ink-3);font-size:11.5px;flex:0 0 auto}
.node.d svg{color:var(--clay)}
.node.f svg{color:#BFC3C2}

@media(max-width:820px){
.top-in,main{padding-left:20px;padding-right:20px}
.bar{height:auto;flex-wrap:wrap;padding:14px 0;gap:14px}
.metrics{width:100%;margin-left:0;gap:22px;justify-content:space-between}
.metric{text-align:left}
.search-row{flex-wrap:wrap}
.field{width:100%;flex:0 0 100%}
.pill{flex:1;justify-content:center;padding:0 16px}
.two{grid-template-columns:1fr}
.sheet{padding:28px 24px}
.summary .tags{margin-left:0;width:100%}
}
</style></head>
<body>

<header class="top">
  <div class="top-in">
    <div class="bar">
      <div class="logo">
        <span class="logo-mark"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><ellipse cx="12" cy="5.5" rx="8.5" ry="3"/><path d="M3.5 5.5v13c0 1.66 3.8 3 8.5 3s8.5-1.34 8.5-3v-13"/><path d="M3.5 12c0 1.66 3.8 3 8.5 3s8.5-1.34 8.5-3"/></svg></span>
        网盘索引库
      </div>
      <nav class="tabs">
        <button class="tab on" id="tabSearch" onclick="switchPage('search')">搜索</button>
        <button class="tab" id="tabSaved" onclick="switchPage('saved')">我的资源库 <span id="savedCount"></span></button>
      </nav>
      <div class="metrics" id="metrics"></div>
    </div>
    <div class="search-row">
      <div class="field">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7.5"/><path d="M20.5 20.5l-4.2-4.2"/></svg>
        <input type="text" class="q" id="q" placeholder="搜索资源 — 例如 AI漫剧、尚硅谷 MySQL、中医（空格分隔多个词）" autocomplete="off">
      </div>
      <button class="pill pill-live" id="matchBtn" onclick="toggleMatch()" title="半匹配：关键词拆分后任一命中即显示（结果更多）">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h10M4 17h6"/></svg>
        <span id="matchLabel">半匹配</span>
      </button>
      <button class="pill pill-live" id="liveBtn" onclick="toggleLive()" title="实时搜索：同时查询 pansou / Telegram（点击关闭）">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/><circle cx="12" cy="12" r="4"/></svg>
        实时
      </button>
      <button class="pill pill-solid" onclick="find(1)">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
        搜索
      </button>
      <button class="pill pill-line" onclick="openDlg('exportVeil')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 3.5v11"/><path d="M7.5 10.5L12 15l4.5-4.5"/><path d="M4 20h16"/></svg>
        导出
      </button>
      <button class="pill pill-line" onclick="openSettings()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M4.2 7.2l2.1 2.1M17.7 14.7l2.1 2.1M2.5 12h3M18.5 12h3M4.2 16.8l2.1-2.1M17.7 9.3l2.1-2.1"/></svg>
        设置
      </button>
    </div>
    <div class="filters" id="filters"></div>
  </div>
</header>

<main>
  <div id="paneSearch">
    <div class="summary">
      <div class="big" id="bigCount"></div>
      <div class="tags" id="summaryTags"></div>
    </div>
    <div id="list"></div>
    <div class="pages" id="pages"></div>
  </div>

  <div id="paneSaved" style="display:none">
    <div class="filterbar" id="savedFilters"></div>
    <div class="summary">
      <div class="big" id="savedBig"></div>
      <div class="tags" id="savedTags"></div>
    </div>
    <div id="savedList"></div>
    <div class="pages" id="savedPages"></div>
  </div>
</main>

<!-- 设置 -->
<div class="veil" id="settingsVeil">
  <div class="sheet" style="max-width:520px">
    <h2>显示设置</h2>
    <div class="grp">
      <div class="lab">网盘来源</div>
      <div class="two" id="diskTicks"></div>
      <div class="note">取消勾选的网盘不会出现在结果中</div>
    </div>
    <div class="grp">
      <div class="lab">搜索源（点击启用/停用）</div>
      <div id="sourceList" style="display:flex;flex-direction:column;gap:8px"></div>
      <div class="note">实时搜索会并行请求所有启用的源，结果自动去重合并</div>
    </div>
    <div class="grp">
      <div class="lab">添加自定义源</div>
      <input type="text" id="nsName" placeholder="名称，例如：我的 pansou">
      <input type="text" id="nsUrl" placeholder="接口地址，用 {kw} 代表关键词" style="margin-top:8px">
      <input type="text" id="nsPath" placeholder="结果数组路径，例如 data.items（默认）" style="margin-top:8px">
      <div class="three" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px">
        <input type="text" id="nfTitle" placeholder="标题字段名" value="title">
        <input type="text" id="nfUrl" placeholder="链接字段名" value="url">
        <input type="text" id="nfCode" placeholder="码字段名" value="code">
      </div>
      <button class="pill pill-line" style="margin-top:10px;width:100%;justify-content:center" onclick="addSource()">+ 添加这个源</button>
    </div>
    <div class="grp">
      <div class="lab">默认行为</div>
      <div class="two">
        <label class="tick on" id="tk_dead"><input type="checkbox" checked onchange="state.hideDead=this.checked;flipTick('tk_dead',this.checked);applyLiveFilter()">隐藏失效链接</label>
      </div>
    </div>
    <div class="sheet-foot">
      <button class="pill pill-line" onclick="closeDlg('settingsVeil')">关闭</button>
    </div>
  </div>
</div>

<!-- 导出 -->
<div class="veil" id="exportVeil">
  <div class="sheet">
    <h2>导出资源清单</h2>
    <div class="grp">
      <div class="lab">文件名</div>
      <input type="text" id="expName" placeholder="资源清单">
      <div class="note">自动附加日期与扩展名</div>
    </div>
    <div class="grp">
      <div class="lab">导出字段</div>
      <div class="two" id="fieldTicks"></div>
    </div>
    <div class="grp two">
      <div>
        <div class="lab">文件格式</div>
        <select id="expFmt">
          <option value="csv">CSV · Excel 打开</option>
          <option value="tsv">TSV · 制表符</option>
          <option value="md">Markdown 表格</option>
        </select>
      </div>
      <div>
        <div class="lab">字段模板</div>
        <select id="expPreset" onchange="usePreset(this.value)">
          <option value="standard">标准</option>
          <option value="simple">精简</option>
          <option value="full">完整</option>
        </select>
      </div>
    </div>
    <div class="note" id="expHint">导出当前筛选条件下的全部结果</div>
    <div class="sheet-foot">
      <button class="pill pill-line" onclick="closeDlg('exportVeil')">取消</button>
      <button class="pill pill-solid" onclick="runExport()">下载</button>
    </div>
  </div>
</div>

<!-- 保存到分类 -->
<div class="veil" id="saveVeil">
  <div class="sheet" style="max-width:520px">
    <h2>保存到资源库</h2>
    <div class="grp">
      <div class="lab">资源</div>
      <div id="saveResName" style="font-size:14px;color:var(--ink);line-height:1.5;
        background:var(--card);border:1px solid var(--line-2);border-radius:11px;padding:12px 14px"></div>
    </div>
    <div class="grp">
      <div class="lab">选择分类</div>
      <div class="two" id="saveTags"></div>
      <div style="display:flex;gap:8px;margin-top:10px">
        <input type="text" id="saveNewTag" placeholder="新建分类名称" style="flex:1">
        <button class="pill pill-line" onclick="saveWithNewTag()" style="padding:0 18px">新建</button>
      </div>
    </div>
    <div class="sheet-foot">
      <button class="pill pill-line" onclick="closeDlg('saveVeil')">取消</button>
      <button class="pill pill-solid" onclick="confirmSave()">保存</button>
    </div>
  </div>
</div>

<!-- 目录树 -->
<div class="veil" id="treeVeil">
  <div class="sheet" style="max-width:740px">
    <h2 id="treeH">目录结构</h2>
    <div id="treeMeta" class="note" style="margin-bottom:16px;margin-top:-10px"></div>
    <div class="tree" id="treeBody"></div>
    <div class="sheet-foot">
      <button class="pill pill-line" onclick="closeDlg('treeVeil')">关闭</button>
    </div>
  </div>
</div>

<script>
const I={
 dir:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7.5A1.5 1.5 0 0 1 4.5 6h4l2 2h9A1.5 1.5 0 0 1 21 9.5v8A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"/></svg>',
 file:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M14 2.5H6.5A1.5 1.5 0 0 0 5 4v16a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 20V7.5z"/><path d="M14 2.5v5h5"/></svg>',
 copy:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="8.5" y="8.5" width="12" height="12" rx="2.5"/><path d="M5 15.5V5a1.5 1.5 0 0 1 1.5-1.5h8"/></svg>',
 tree:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3.5 4.5h5v3.5h-5zM3.5 16h5v3.5h-5zM15 10.5h5.5V14H15z"/><path d="M8.5 6.25h3v11.5h3.5M11.5 6.25v6"/></svg>',
 blank:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2"><circle cx="11" cy="11" r="7.5"/><path d="M21 21l-4.5-4.5"/><path d="M8.5 11h5"/></svg>',
 plus:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 5.5v13M5.5 12h13"/></svg>',
 check:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M4 12.5l5 5L20 6.5"/></svg>',
 trash:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4.5 7h15M9 7V4.5h6V7M6.5 7l1 12.5h9L17.5 7"/></svg>'
};
let state={q:'',disk:'',usable:false,only_good:false,access:'',dedup:true,has_size:false,
  min_gb:'',max_gb:'',date_from:'',date_to:'',sort:'smart',page:1,hideDead:true,showDisks:null,
  live:true, liveSources:['pansou','tg'], match:'loose', view:'search', savedPage:1, savedQ:'', savedTag:'', onlyDead:false};
const FIELDS=[['name','资源名称'],['disk','网盘'],['url','网盘链接'],['code','提取码'],
  ['size','文件大小'],['share_time','分享时间'],['probe','内容体检'],['valid','有效性'],
  ['source','来源'],['first_seen','收录时间']];
const PRESETS={simple:['name','disk','url','code'],
  standard:['name','disk','url','code','size','share_time'],
  full:['name','disk','url','code','size','share_time','probe','valid','source','first_seen']};
const ACC={accessible:['s-ok','可直接获取'],with_code:['s-sage','凭码可获取'],bad_code:['s-warn','码错误'],
  no_code:['s-warn','缺码'],dead:['s-bad','已失效'],unknown:['','未验证'],magnet:['','磁力']};

const esc=s=>(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gb=mb=>!mb?'':(mb>=1024?(mb/1024).toFixed(2)+'GB':mb+'MB');
const flipTick=(id,on)=>document.getElementById(id).classList.toggle('on',on);
function openDlg(id){document.getElementById(id).classList.add('show')}
function closeDlg(id){document.getElementById(id).classList.remove('show')}

function accBadge(a){
  if(!a||a==='unknown')return '';
  const[c,l]=ACC[a]||['',a];
  return '<span class="b '+c+'">'+l+'</span>';
}
function probeBadge(p){
  if(!p)return '';
  if(p.startsWith('✅')||p.startsWith('🏆'))return '<span class="b s-ok">'+esc(p.slice(1))+'</span>';
  if(p.startsWith('🟡'))return '<span class="b s-sage">'+esc(p.slice(1))+'</span>';
  if(p.startsWith('📄'))return '<span class="b">'+esc(p.slice(1))+'</span>';
  if(p.startsWith('⚠️'))return '<span class="b s-warn">'+esc(p.slice(2))+'</span>';
  return '<span class="b">'+esc(p)+'</span>';
}

function switchPage(v){
  state.view=v;
  document.getElementById('paneSearch').style.display=v==='search'?'':'none';
  document.getElementById('paneSaved').style.display=v==='saved'?'':'none';
  document.getElementById('tabSearch').classList.toggle('on',v==='search');
  document.getElementById('tabSaved').classList.toggle('on',v==='saved');
  // 搜索页专属控件
  document.getElementById('filters').style.display='none';  // 搜索页不做本地检索，无需这些筛选
  document.getElementById('liveBtn').style.display=v==='search'?'':'none';
  // 搜索框 placeholder 按页面区分
  const q=document.getElementById('q');
  q.placeholder=v==='search'
    ?'搜索资源 — 例如 AI漫剧、尚硅谷 MySQL、中医（空格分隔多个词）'
    :'在我的资源库中搜索…';
  q.value='';
  syncPad();
  if(v==='saved') loadSaved(1);
  else document.getElementById('q').value=state.q;
}
let _saveTarget=null, _saveTags=[];
async function saveIt(btn){
  const it=(window._itemMap&&window._itemMap[btn.dataset.url])
        ||(window._lastItems||[]).find(x=>x.url===btn.dataset.url);
  if(!it)return;
  _saveTarget={item:it, btn:btn};
  _saveTags=[];
  const nameEl=document.getElementById('saveResName');
  nameEl.textContent=it.title||it.raw_title||'';
  nameEl.dataset.custom='';
  document.getElementById('saveTags').innerHTML='<span style="font-size:13px;color:var(--ink-3)">加载分类…</span>';
  document.getElementById('saveNewTag').value='';
  openDlg('saveVeil');   // 立即弹出，分类列表随后异步填充
  try{
    const d=await(await fetch('/api/saved/stats')).json();
    const tags=(d.tags||[]).map(t=>t.name).filter(Boolean);
    const presets=['未分类','课程清单','闲鱼清单','AI·AI漫剧','AI·AI工具教程','AI·AI提示词',
                   'AI·AI数字人','AI·AI生图视频','AI·AI写作','AI·AI配音'];
    const all=[...new Set([...presets,...tags])];
    document.getElementById('saveTags').innerHTML=all.map((t,i)=>
      '<label class="tick" id="st_'+i+'" onclick="toggleSaveTag(this)">'+
      '<input type="checkbox" value="'+esc(t)+'">'+esc(t.replace(/^AI·/,''))+'</label>').join('');
  }catch(e){
    document.getElementById('saveTags').innerHTML='';
  }
}
function toggleSaveTag(el){
  const cb=el.querySelector('input');
  const tag=cb.value;
  const on=!cb.checked;
  cb.checked=on;
  el.classList.toggle('on',on);
  if(on){ if(!_saveTags.includes(tag)) _saveTags.push(tag); }
  else _saveTags=_saveTags.filter(x=>x!==tag);
}
async function saveWithNewTag(){
  const v=document.getElementById('saveNewTag').value.trim();
  if(!v)return;
  _saveTags=[v];
  const box=document.getElementById('saveTags');
  box.querySelectorAll('.tick').forEach(el=>{
    el.classList.remove('on'); el.querySelector('input').checked=false;
  });
  // 立即把新分类显示出来并选中，不再等下一次弹窗
  box.insertAdjacentHTML('beforeend',
    '<label class="tick on" id="st_new" onclick="toggleSaveTag(this)">'+
    '<input type="checkbox" checked value="'+esc(v)+'">'+esc(v.replace(/^AI·/,''))+'</label>');
  document.getElementById('saveNewTag').value='';
  document.getElementById('saveResName').dataset.custom=v;
  try{ await fetch('/api/saved/tag?name='+encodeURIComponent(v)); }catch(e){}
}
async function confirmSave(){
  if(!_saveTarget) return;
  const it=_saveTarget.item;
  const cbtn=document.querySelector('#saveVeil .sheet-foot button.pill-solid');
  if(cbtn){ cbtn.disabled=true; cbtn.textContent='保存中…'; }
  const custom=document.getElementById('saveResName').dataset.custom;
  const tags=_saveTags.length?_saveTags.join(','):(custom||'未分类');
  const q=new URLSearchParams({url:it.url,title:it.raw_title||it.title,disk:it.disk,
    code:it.code||'',size_mb:it.size_mb||'',share_time:it.share_time||'',source:it.source||'',
    tags:tags});
  try{
    await fetch('/api/saved/save?'+q);
  }catch(e){
    if(cbtn){ cbtn.disabled=false; cbtn.textContent='保存'; }
    return;
  }
  it.saved=true;
  if(_saveTarget.btn)
    _saveTarget.btn.outerHTML='<button class="mini saved" data-url="'+esc(it.url)+'" onclick="unsave(this)">'+I.check+'已保存</button>';
  closeDlg('saveVeil');
  if(cbtn){ cbtn.disabled=false; cbtn.textContent='保存'; }
  refreshSavedCount();
  _saveTarget=null;
}
async function unsave(btn){
  await fetch('/api/saved/remove?url='+encodeURIComponent(btn.dataset.url));
  const it=window._itemMap&&window._itemMap[btn.dataset.url];
  if(it) it.saved=false;
  btn.outerHTML='<button class="mini" data-url="'+esc(btn.dataset.url)+'" onclick="saveIt(this)">'+I.plus+'保存</button>';
  refreshSavedCount();
  if(state.view==='saved') loadSaved(state.savedPage);
}
async function refreshSavedCount(){
  try{
    const d=await(await fetch('/api/saved/stats')).json();
    const el=document.getElementById('savedCount');
    if(el)el.textContent=d.total?'('+d.total+')':'';
  }catch(e){}
}
async function paintSavedFilters(){
  try{
    const d=await(await fetch('/api/saved/stats')).json();
    window._savedStats=d;
    const LISTS=['闲鱼清单','课程清单'];
    const tags=(d.tags||[]);
    const lists=tags.filter(t=>LISTS.includes(t.name));
    const cats=tags.filter(t=>!LISTS.includes(t.name));
    let h='<span class="chip '+(state.savedTag===''?'on':'')+'" onclick="pickSavedTag(&quot;&quot;)">全部 '+d.total+'</span>';
    if(lists.length){
      h+='<span class="divider"></span><span class="flabel">清单</span>';
      for(const t of lists)
        h+='<span class="chip '+(state.savedTag===t.name?'on':'')+'" onclick="pickSavedTag(&quot;'+esc(t.name)+'&quot;)">'+esc(t.name)+'<span class="cnt">'+t.n+'</span></span>';
    }
    if(cats.length){
      h+='<span class="divider"></span><span class="flabel">分类</span>';
      for(const t of cats)
        h+='<span class="chip '+(state.savedTag===t.name?'on':'')+'" onclick="pickSavedTag(&quot;'+esc(t.name)+'&quot;)">'+esc(t.name.replace(/^AI·/,''))+'<span class="cnt">'+t.n+'</span></span>';
    }
    if(d.dead){
      h+='<span class="divider"></span>';
      h+='<span class="chip bad '+(state.onlyDead?'on':'')+'" onclick="toggleDead()">✕ 已失效<span class="cnt">'+d.dead+'</span></span>';
    }
    if(d.disks&&d.disks.length){
      h+='<span class="divider"></span><span class="flabel">网盘</span>';
      for(const t of d.disks)
        h+='<span class="chip '+(state.disk===t.name?'on':'')+'" onclick="pickDiskSaved(&quot;'+esc(t.name)+'&quot;)">'+esc(t.name)+'<span class="cnt">'+t.n+'</span></span>';
    }
    document.getElementById('savedFilters').innerHTML=h;
  }catch(e){}
}
function pickDiskSaved(d){state.disk=d;paintSavedFilters();loadSaved(1)}
function toggleDead(){state.onlyDead=!state.onlyDead;paintSavedFilters();loadSaved(1)}
function pickSavedTag(t){state.savedTag=t;paintSavedFilters();loadSaved(1)}
async function loadSaved(pg){
  state.savedPage=pg||1;
  paintSavedFilters();
  const q=new URLSearchParams({q:state.savedQ,page:state.savedPage,size:25,
    disk:state.disk,sort:state.sort,tag:state.savedTag||'',
    only_dead:state.onlyDead?1:0,match:state.match});
  const d=await(await fetch('/api/saved/list?'+q)).json();
  document.getElementById('savedBig').innerHTML='我的资源库 · <b>'+d.total+'</b> 个';
  document.getElementById('savedTags').innerHTML=
    (state.disk?'<span class="tag">'+esc(state.disk)+'</span>':'')+
    '<span class="tag">第 '+d.page+' 页</span>';
  const box=document.getElementById('savedList');
  if(!d.items.length){
    box.innerHTML='<div class="blank">'+I.blank+'<div class="h">资源库还是空的</div>'+
      '<div class="p">在「搜索」页找到资源后点「保存」，就会收进这里</div></div>';
    document.getElementById('savedPages').innerHTML='';return;
  }
  box.innerHTML=d.items.map(it=>{
    const bits=['<span class="b s-sage">'+esc(it.disk||'?')+'</span>'];
    if(it.code)bits.push('<span class="b s-clay">码 '+esc(it.code)+'</span>');
    if(it.size_mb)bits.push('<span class="b">'+gb(it.size_mb)+'</span>');
    if(it.saved_at)bits.push('<span class="b">'+esc(it.saved_at.slice(0,10))+'</span>');
    if(it.tags)bits.push('<span class="b s-sage">'+esc(it.tags)+'</span>');
    if(it.probe)bits.push(probeBadge(it.probe));
    return '<article class="item">'+
      '<h3 class="t" title="'+esc(it.raw_title)+'">'+esc(it.title)+'</h3>'+
      '<div class="meta-row">'+bits.join('')+'</div>'+
      '<div class="link-row"><a class="url" href="'+esc(it.url)+'" target="_blank" rel="noopener">'+esc(it.url)+'</a>'+
        '<button class="mini" data-url="'+esc(it.url)+'" onclick="copyLink(this)">'+I.copy+'复制</button>'+
        '<button class="mini" data-url="'+esc(it.url)+'" onclick="showTree(this)">'+I.tree+'目录</button>'+
        '<button class="mini del" data-url="'+esc(it.url)+'" onclick="unsave(this)">'+I.trash+'移除</button></div>'+
      (it.note?'<div class="source">'+esc(it.note)+'</div>':'')+'</article>';
  }).join('');
  const tot=Math.ceil(d.total/d.size);let h='';
  if(tot>1){
    h+=(d.page>1?'<button class="pg" onclick="loadSaved('+(d.page-1)+')">← 上一页</button>':'');
    h+='<span class="now">'+d.page+' / '+tot+'</span>';
    h+=(d.page<tot?'<button class="pg" onclick="loadSaved('+(d.page+1)+')">下一页 →</button>':'');
  }
  document.getElementById('savedPages').innerHTML=h;
}
async function stats(){
  const s=await(await fetch('/api/stats')).json();
  document.getElementById('metrics').innerHTML=
    '<div class="metric hi"><span class="n">'+s.usable+'</span><span class="l">可获取</span></div>'+
    '<div class="metric"><span class="n">'+s.unique+'</span><span class="l">去重资源</span></div>'+
    '<div class="metric"><span class="n">'+s.coded+'</span><span class="l">带提取码</span></div>'+
    '<div class="metric"><span class="n">'+s.good+'</span><span class="l">内容完整</span></div>';
  window._disks=s.disks;
  paintFilters(); syncPad();
}
function paintFilters(){
  // 搜索页只做实时检索，不渲染本地筛选条
  const box=document.getElementById('filters');
  if(box){ box.innerHTML=''; box.style.display='none'; }
  return;
  const f=window._facets||{};
  const order=['夸克','百度','阿里','123盘','天翼','迅雷','115','UC'];
  let h='<span class="chip '+(state.disk===''?'on':'')+'" onclick="pickDisk(&quot;&quot;)">全部<span class="cnt">'+(window._curTotal||'')+'</span></span>';
  for(const name of order){
    const n=f[name];
    if(!n) continue;
    h+='<span class="chip '+(state.disk===name?'on':'')+'" onclick="pickDisk(&quot;'+name+'&quot;)">'+name+'<span class="cnt">'+n+'</span></span>';
  }
  h+='<span class="divider"></span>';
  h+='<span class="chip only '+(state.usable?'on':'')+'" onclick="flip(&quot;usable&quot;)" title="可直接获取 + 凭码可获取">✓ 可获取</span>';
  h+='<span class="chip '+(state.only_good?'on':'')+'" onclick="flip(&quot;only_good&quot;)">内容完整</span>';

  h+='<span class="divider"></span>';
  h+='<span class="range">大小 <input type="number" placeholder="最小" value="'+esc(state.min_gb)+'" onchange="state.min_gb=this.value;find(1)"> — <input type="number" placeholder="最大" value="'+esc(state.max_gb)+'" onchange="state.max_gb=this.value;find(1)"> GB</span>';
  h+='<span class="range">时间 <input type="date" value="'+esc(state.date_from)+'" onchange="state.date_from=this.value;find(1)"> — <input type="date" value="'+esc(state.date_to)+'" onchange="state.date_to=this.value;find(1)"></span>';
  h+='<select class="sort" onchange="state.sort=this.value;find(1)">'+
    [['smart','智能排序'],['recent','最新分享'],['size_desc','体积从大到小'],['size_asc','体积从小到大'],['title','按标题'],['disk','按网盘']]
    .map(([k,v])=>'<option value="'+k+'"'+(state.sort===k?' selected':'')+'>'+v+'</option>').join('')+'</select>';
  document.getElementById('filters').innerHTML=h;
}
function toggleMatch(){
  state.match = state.match === 'exact' ? 'loose' : 'exact';
  const b = document.getElementById('matchBtn');
  const lb = document.getElementById('matchLabel');
  if(state.match === 'loose'){
    b.classList.remove('pill-ghost'); b.classList.add('pill-live');
    lb.textContent = '半匹配';
    b.title = '半匹配：关键词拆分后任一命中即显示（结果更多，相关性略低）';
  }else{
    b.classList.add('pill-ghost'); b.classList.remove('pill-live');
    lb.textContent = '全匹配';
    b.title = '全匹配：整串关键词必须连续出现（结果精准）';
  }
  find(1);
}
let _liveBusy=false;
function toggleLive(){
  if(_liveBusy) return;
  _liveBusy=true;
  state.live=!state.live;
  const b=document.getElementById('liveBtn');
  // 立即反馈
  b.className='pill '+(state.live?'pill-live':'pill-ghost');
  b.title=state.live?'实时搜索已开启（点击关闭）':'实时搜索已关闭（点击开启）';
  if(!state.live){
    const lb=document.getElementById('livebar');
    if(lb) lb.remove();
    const lw=document.getElementById('liveWrap');
    if(lw) lw.remove();
    _liveSeq++;   // 取消进行中的流
    window._liveTotal=0;
    updateBigCount();
    _liveBusy=false;
    return;
  }
  find(1).finally(()=>{_liveBusy=false});
}
function pickDisk(d){state.disk=d;paintFilters();find(1)}
function flip(k){state[k]=!state[k];paintFilters();find(1)}
function query(){
  return new URLSearchParams({q:state.q,disk:state.disk,sort:state.sort,page:state.page,size:25,
    usable:state.usable?1:0,only_good:state.only_good?1:0,access:state.access,match:state.match,
    dedup:state.dedup?1:0,has_size:state.has_size?1:0,min_gb:state.min_gb,max_gb:state.max_gb,
    date_from:state.date_from,date_to:state.date_to,hide_dead:state.hideDead?1:0,
    disks:(state.showDisks||[]).join(','),live:state.live?1:0,
    live_sources:(state.liveSources||[]).join(','),skip_local:1});
}
function updateBigCount(){
  const liv=window._liveTotal||0;
  const el=document.getElementById('bigCount');
  if(!el) return;
  if(liv>0){
    el.innerHTML='找到 <b>'+liv+'</b> 条资源';
  }else if(state.live&&state.q&&window._liveStreaming){
    el.innerHTML='<span style="color:var(--ink-3);font-weight:400">正在实时检索…</span>';
  }else if(state.q){
    el.innerHTML='找到 <b>0</b> 条资源';
  }else{
    el.innerHTML='';
  }
}
function cardHtml(it){
  const cls=it.access==='dead'?'item gone':'item';
  const bits=[accBadge(it.access),'<span class="b s-sage">'+esc(it.disk||'?')+'</span>'];
  if(it.code)bits.push('<span class="b s-clay">码 '+esc(it.code)+'</span>');
  if(it.size_mb)bits.push('<span class="b">'+gb(it.size_mb)+'</span>');
  if(it.share_time)bits.push('<span class="b">'+esc((it.share_time||'').slice(0,10))+'</span>');
  if(it.probe)bits.push(probeBadge(it.probe));
  if(it.tags)bits.push('<span class="b s-sage">'+esc(it.tags)+'</span>');
  if(it.dups>1)bits.push('<span class="b">同款 '+it.dups+'</span>');
  if(it.live)bits.push('<span class="b s-clay">实时</span>');
  const saveBtn = it.saved
    ? '<button class="mini saved" title="已保存到资源库，点击取消保存" data-url="'+esc(it.url)+
      '" onclick="unsave(this)">'+I.check+'已保存</button>'
    : '<button class="mini" data-url="'+esc(it.url)+'" onclick="saveIt(this)">'+I.plus+'保存</button>';
  return '<article class="'+cls+'">'+
    '<h3 class="t" title="'+esc(it.raw_title||it.title)+'">'+esc(it.title)+'</h3>'+
    '<div class="meta-row">'+bits.join('')+'</div>'+
    '<div class="link-row"><a class="url" href="'+esc(it.url)+'" target="_blank" rel="noopener">'+esc(it.url)+'</a>'+
      '<button class="mini" data-url="'+esc(it.url)+'" onclick="copyLink(this)">'+I.copy+'复制</button>'+
      '<button class="mini" data-url="'+esc(it.url)+'" onclick="showTree(this)">'+I.tree+'目录</button>'+
      saveBtn+'</div>'+
    '<div class="source">'+esc((it.source||'').slice(0,72))+'</div></article>';
}

async function find(pg){
  state.page=pg||1;
  state.q=document.getElementById('q').value.trim();
  const d=await(await fetch('/api/search?'+query())).json();
  window._facets=d.facets||{};
  window._curTotal=d.total;
  paintFilters();
  window._liveTotal=0;
  window._liveItems=[];
  window._liveStreaming=false;
  updateBigCount();
  const tags=[];
  if(state.q)tags.push('关键词「'+state.q+'」');
  tags.push(state.match==='loose'?'半匹配':'全匹配');
  tags.push(state.live?'实时检索':'未开启实时');
  document.getElementById('summaryTags').innerHTML=tags.map(t=>'<span class="tag">'+esc(t)+'</span>').join('');
  const list=document.getElementById('list');
  window._itemMap={};
  window._lastItems=d.items||[];
  window._lastItems.forEach(x=>window._itemMap[x.url]=x);
  if(!window._lastItems.length){
    list.innerHTML = (state.live&&state.q)
      ? ''
      : '<div class="blank">'+I.blank+'<div class="h">没有找到匹配的资源</div><div class="p">换个关键词，或者放宽筛选条件试试</div></div>';
    document.getElementById('pages').innerHTML='';
  }else{
    list.innerHTML=window._lastItems.map(cardHtml).join('');
    const tot=Math.ceil(d.total/d.size);let h='';
    if(tot>1){
      h+=(d.page>1?'<button class="pg" onclick="find('+(d.page-1)+')">← 上一页</button>':'');
      h+='<span class="now">'+d.page+' / '+tot+'</span>';
      h+=(d.page<tot?'<button class="pg" onclick="find('+(d.page+1)+')">下一页 →</button>':'');
    }
    document.getElementById('pages').innerHTML=h;
  }
  if(state.live && state.q){
    // 保留已有的实时结果，重新插入（避免分页后丢失）
    loadLive(state.q);
  }
}

let _liveSeq=0;
async function loadLive(kw){
  const seq=++_liveSeq;
  const srcSel=(state.liveSources||['pansou','tg']).join(',');
  window._streamSources=srcSel.split(',');
  window._liveItems=[];
  window._liveStreaming=true;
  const list=document.getElementById('list');
  const bar=document.createElement('div');
  bar.id='livebar'; bar.className='livebar';
  bar.innerHTML='<span class="spin"></span> 正在检索实时源…';
  list.insertBefore(bar, list.firstChild);

  // 已展示的 URL（本地结果 + 实时已插入的）
  const shown=new Set((window._lastItems||[]).map(x=>x.url));
  let added=0, srcCount={};
  const wrap=document.createElement('div');
  wrap.id='liveWrap';
  bar.insertAdjacentElement('afterend', wrap);

  try{
    // 把本地已展示的标题传给后端，避免实时结果重复
    const localTitles=(window._lastItems||[]).map(x=>x.title).slice(0,200);
    const es=new EventSource('/api/live/stream?q='+encodeURIComponent(kw)+
      '&sources='+srcSel+'&match='+state.match+
      '&lt='+encodeURIComponent(localTitles.join('')));
    es.onmessage = (ev)=>{
      if(seq!==_liveSeq){ es.close(); return; }
      let d;
      try{ d=JSON.parse(ev.data); }catch(e){ return; }
      if(d.done){
        es.close();
        window._liveStreaming=false;
        const b=document.getElementById('livebar');
        const all=(window._liveItems||[]).length;
        const vis=(window._liveItems||[]).filter(liveVisible).length;
        if(b) b.innerHTML = all
          ? '实时源获取 <b>'+all+'</b> 条 · '+Object.entries(srcCount).map(([k,v])=>esc(k)+' +'+v).join(' · ')+
            (vis!==all?' · 当前显示 <b>'+vis+'</b> 条':'')
          : '实时源无新增结果';
        if(!all && !(window._lastItems||[]).length){
          list.insertAdjacentHTML('beforeend','<div class="blank">'+I.blank+'<div class="h">没有找到匹配的资源</div><div class="p">换个关键词，或者放宽匹配模式试试</div></div>');
        }
        updateBigCount();
        return;
      }
      const fresh=(d.items||[]).filter(x=>!shown.has(x.url));
      srcCount[d.source]=(srcCount[d.source]||0)+fresh.length;
      if(!fresh.length) return;
      fresh.forEach(x=>{shown.add(x.url); x._src=d.source; window._itemMap[x.url]=x; window._liveItems.push(x);});
      added+=fresh.length;
      const vis=fresh.filter(liveVisible);
      window._liveTotal=(window._liveTotal||0)+vis.length;
      updateBigCount();
      // 边到边插（只插入当前筛选条件可见的）
      if(vis.length){
        const div=document.createElement('div');
        div.innerHTML=vis.map(cardHtml).join('');
        const cur=document.getElementById('liveWrap');
        if(cur) cur.appendChild(div);
      }
      const b=document.getElementById('livebar');
      if(b){
        const all=(window._liveItems||[]).length;
        const shownNow=(window._liveItems||[]).filter(liveVisible).length;
        b.innerHTML='<span class="spin"></span> 实时源已获取 <b>'+all+'</b> 条'+
          (shownNow!==all?' · 当前显示 '+shownNow+' 条':'')+'，继续检索…';
      }
    };
    es.onerror = ()=>{
      es.close();
      window._liveStreaming=false;
      const b=document.getElementById('livebar');
      if(b && seq===_liveSeq) b.innerHTML='实时检索结束（共 '+(window._liveItems||[]).length+' 条）';
      updateBigCount();
    };
  }catch(e){
    window._liveStreaming=false;
    const b=document.getElementById('livebar');
    if(b) b.innerHTML='实时源请求失败';
  }
}

// 已收到的实时结果按当前勾选的「网盘来源 / 搜索源」即时过滤，不重新请求
function liveVisible(x){
  const dk=state.showDisks;
  if(dk && !dk.includes(x.disk)) return false;
  const srcs=state.liveSources||[];
  if(x._src && !srcs.includes(x._src)) return false;
  return true;
}
function applyLiveFilter(){
  const wrap=document.getElementById('liveWrap');
  if(!wrap) return;
  const all=window._liveItems||[];
  const vis=all.filter(liveVisible);
  wrap.innerHTML=vis.length
    ? vis.map(cardHtml).join('')
    : (window._liveStreaming?'':'<div class="blank" style="padding:30px 0"><div class="h" style="font-size:15px">当前筛选下没有结果</div><div class="p">把网盘来源或搜索源重新勾选回来即可显示</div></div>');
  window._liveTotal=vis.length;
  updateBigCount();
  const b=document.getElementById('livebar');
  if(b){
    const pending=(state.liveSources||[]).filter(k=>!(window._streamSources||[]).includes(k));
    b.innerHTML=(window._liveStreaming?'<span class="spin"></span> ':'')+
      '已获取 '+all.length+' 条 · 当前显示 '+vis.length+' 条'+
      (pending.length?' · 新启用的来源将在下次搜索生效':'');
  }
}

function copyLink(btn){
  const t=btn.dataset.url;const orig=btn.innerHTML;
  const ok=()=>{btn.textContent='已复制';setTimeout(()=>btn.innerHTML=orig,1400)};
  if(navigator.clipboard&&isSecureContext){navigator.clipboard.writeText(t).then(ok).catch(fb)}else fb();
  function fb(){const ta=document.createElement('textarea');ta.value=t;ta.style.cssText='position:fixed;opacity:0';
    document.body.appendChild(ta);ta.select();try{document.execCommand('copy');ok()}catch(e){}document.body.removeChild(ta)}
}
const sz=b=>!b?'':(b>=1073741824?(b/1073741824).toFixed(2)+'GB':b>=1048576?(b/1048576).toFixed(0)+'MB':Math.max(1,Math.round(b/1024))+'KB');
function drawTree(nodes,depth){
  let h='';const pad=depth*17+'px';
  for(const n of nodes){
    const cnt=(n.dir&&n.count!==undefined)?'<span class="m">'+n.count+' 项</span>':'';
    const s=(!n.dir&&n.size)?'<span class="m">'+sz(n.size)+'</span>':'';
    const e=n.error?'<span class="m" style="color:var(--bad)">读取失败</span>':'';
    h+='<div class="node '+(n.dir?'d':'f')+'" style="padding-left:'+pad+'">'+(n.dir?I.dir:I.file)+
       '<span class="nm">'+esc(n.name)+'</span>'+cnt+s+e+'</div>';
    if(n.children&&n.children.length)h+=drawTree(n.children,depth+1);
  }
  return h;
}
async function showTree(btn){
  const url=btn.dataset.url;
  document.getElementById('treeH').textContent='目录结构';
  document.getElementById('treeMeta').textContent='正在读取，首次约需 3–10 秒…';
  document.getElementById('treeBody').innerHTML='';
  openDlg('treeVeil');
  try{
    const r=await(await fetch('/api/tree?url='+encodeURIComponent(url))).json();
    if(!r.ok){
      document.getElementById('treeMeta').innerHTML='<span style="color:var(--bad)">无法读取：'+esc(r.err||'未知')+(r.need_code?'（需要提取码）':'')+'</span>';
      return;
    }
    document.getElementById('treeMeta').innerHTML='<span class="b s-ok">'+esc(r.grade||'')+'</span> '+
      '<span class="b">'+r.files+' 文件</span> <span class="b">'+r.dirs+' 目录</span> '+
      '<span class="b">视频 '+r.videos+'</span> '+
      (r.truncated?'<span class="b s-warn">已截断</span>':'<span class="b s-ok">完整</span>');
    document.getElementById('treeBody').innerHTML=drawTree(r.tree||[],0)||'<div style="color:var(--ink-3)">（空目录）</div>';
  }catch(e){
    document.getElementById('treeMeta').innerHTML='<span style="color:var(--bad)">请求失败</span>';
  }
}
function openSettings(){
  const ds=(window._disks||[]).filter(d=>d.name!=='磁力'&&d.name!=='others'&&d.name!=='tianyi').map(d=>d.name);
  const cur=state.showDisks;
  document.getElementById('diskTicks').innerHTML=ds.map(n=>{
    const on=cur===null||cur.includes(n);
    return '<label class="tick '+(on?'on':'')+'" id="dt_'+n+'"><input type="checkbox" '+(on?'checked':'')+
      ' onchange="toggleDisk(&quot;'+n+'&quot;,this.checked)">'+n+'</label>';
  }).join('');
  loadSources();
  openDlg('settingsVeil');
}
async function loadSources(){
  try{
    const d=await(await fetch('/api/sources')).json();
    window._allSources=d.sources;
    const box=document.getElementById('sourceList');
    box.innerHTML=Object.entries(d.sources).map(([k,v])=>{
      if(k==='local')return '';
      const on=state.liveSources.includes(k);
      const kindLabel={pansou:'自建',json:'公共',tg:'TG'}[v.kind]||v.kind;
      return '<div class="srcrow '+(on?'on':'')+'" id="sc_'+k+'">'+
        '<label style="display:flex;align-items:center;gap:10px;flex:1;cursor:pointer">'+
        '<input type="checkbox" '+(on?'checked':'')+' onchange="toggleSource(&quot;'+k+'&quot;,this.checked)" style="accent-color:var(--sage)">'+
        '<span style="font-weight:500">'+esc(v.name)+'</span>'+
        '<span class="b">'+kindLabel+'</span></label>'+
        '<div class="srcd">'+esc(v.desc||'')+'</div></div>';
    }).join('');
  }catch(e){}
}
async function addSource(){
  const name=document.getElementById('nsName').value.trim();
  const url=document.getElementById('nsUrl').value.trim();
  if(!name||!url){alert('名称和接口地址必填');return}
  const key='custom_'+Date.now();
  window._allSources[key]={
    name:name,kind:'json',enabled:true,url:url,
    list_path:document.getElementById('nsPath').value.trim()||'data.items',
    fields:{title:document.getElementById('nfTitle').value.trim()||'title',
            url:document.getElementById('nfUrl').value.trim()||'url',
            code:document.getElementById('nfCode').value.trim()||'code'},
    desc:'自定义源'
  };
  await fetch('/api/sources/save?config='+encodeURIComponent(JSON.stringify(window._allSources)));
  state.liveSources.push(key);
  document.getElementById('nsName').value='';
  document.getElementById('nsUrl').value='';
  loadSources();
}
async function toggleSource(k,on){
  let cur=state.liveSources.slice();
  if(on){if(!cur.includes(k))cur.push(k)}else{cur=cur.filter(x=>x!==k)}
  state.liveSources=cur;
  const row=document.getElementById('sc_'+k);
  if(row)row.classList.toggle('on',on);
  applyLiveFilter();   // 立即过滤已出的结果
  if(window._allSources&&window._allSources[k]){
    window._allSources[k].enabled=on;
    await fetch('/api/sources/save?config='+encodeURIComponent(JSON.stringify(window._allSources)));
  }
}
function toggleDisk(name,on){
  const all=(window._disks||[]).filter(d=>d.name!=='磁力'&&d.name!=='others'&&d.name!=='tianyi').map(d=>d.name);
  let cur=state.showDisks===null?all.slice():state.showDisks.slice();
  if(on){if(!cur.includes(name))cur.push(name)}else{cur=cur.filter(x=>x!==name)}
  state.showDisks=cur.length===all.length?null:cur;
  flipTick('dt_'+name,on);
  applyLiveFilter();   // 只过滤已出的结果，不重新搜索
}
function openExport(){
  const box=document.getElementById('fieldTicks');box.innerHTML='';
  const pre=PRESETS[document.getElementById('expPreset').value]||PRESETS.standard;
  FIELDS.forEach(([k,l])=>{
    const on=pre.includes(k);
    box.insertAdjacentHTML('beforeend','<label class="tick '+(on?'on':'')+'" id="ft_'+k+
      '"><input type="checkbox" id="fx_'+k+'" '+(on?'checked':'')+
      ' onchange="flipTick(\'ft_'+k+'\',this.checked)">'+l+'</label>');
  });
  document.getElementById('expName').value=state.q?state.q+'_资源清单':'资源清单';
  openDlg('exportVeil');
}
function usePreset(p){
  const set=PRESETS[p]||PRESETS.standard;
  FIELDS.forEach(([k])=>{
    const cb=document.getElementById('fx_'+k);if(!cb)return;
    cb.checked=set.includes(k);flipTick('ft_'+k,cb.checked);
  });
}
function runExport(){
  const fs=FIELDS.map(([k])=>k).filter(k=>document.getElementById('fx_'+k).checked);
  if(!fs.length){alert('至少选择一个字段');return}
  const nm=(document.getElementById('expName').value||'资源清单').replace(/[\\/:*?"<>|]/g,'_');
  const fm=document.getElementById('expFmt').value;
  const p=query();p.set('fields',fs.join(','));p.set('fmt',fm);p.delete('page');p.delete('size');
  const a=document.createElement('a');
  a.href='/api/export?'+p.toString();
  a.download=nm+'_'+new Date().toISOString().slice(0,10).replace(/-/g,'')+'.'+fm;
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  closeDlg('exportVeil');
}
function syncPad(){
  requestAnimationFrame(()=>{
    const h=document.querySelector('.top').offsetHeight;
    document.querySelector('main').style.paddingTop=(h+16)+'px';
    document.querySelectorAll('.summary').forEach(sm=>{sm.style.top=h+'px'});
    const fb=document.getElementById('savedFilters');
    if(fb)fb.style.top=(h-2)+'px';
  });
}
document.getElementById('q').addEventListener('keydown',e=>{
  if(e.key!=='Enter')return;
  if(state.view==='saved'){state.savedQ=document.getElementById('q').value.trim();loadSaved(1)}
  else find(1);
});
document.getElementById('saveNewTag').addEventListener('keydown',e=>{
  if(e.key==='Enter'){ e.preventDefault(); saveWithNewTag(); }
});
// 吸附条位置跟随顶栏高度
const ro=new ResizeObserver(()=>syncPad());
window.addEventListener('load',()=>{const t=document.querySelector('.top');if(t)ro.observe(t)});
document.querySelectorAll('.veil').forEach(v=>v.addEventListener('click',e=>{if(e.target===v)v.classList.remove('show')}));
window.addEventListener('resize',syncPad);
stats().then(()=>{
  syncPad();
  // 同步按钮初始态
  const lb=document.getElementById('liveBtn');
  if(lb) lb.className='pill '+(state.live?'pill-live':'pill-ghost');
  const mb=document.getElementById('matchBtn');
  const ml=document.getElementById('matchLabel');
  if(mb){mb.className='pill '+(state.match==='loose'?'pill-live':'pill-ghost');}
  if(ml) ml.textContent = state.match==='loose'?'半匹配':'全匹配';
  find(1);
  refreshSavedCount();
});


setTimeout(syncPad,400);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        data = body if isinstance(body, bytes) else body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ('/', '/index.html'):
                self._send(200, PAGE, 'text/html; charset=utf-8')
            elif u.path == '/api/stats':
                self._send(200, json.dumps(do_stats(), ensure_ascii=False))
            elif u.path == '/api/search':
                self._send(200, json.dumps(do_search(p), ensure_ascii=False))
            elif u.path == '/api/saved/list':
                self._send(200, json.dumps(do_saved_list(p), ensure_ascii=False))
            elif u.path == '/api/saved/dead':
                self._send(200, json.dumps(do_dead_list(p), ensure_ascii=False))
            elif u.path == '/api/saved/stats':
                self._send(200, json.dumps(do_saved_stats(), ensure_ascii=False))
            elif u.path == '/api/saved/tag':
                self._send(200, json.dumps(do_tag_create(p), ensure_ascii=False))
            elif u.path == '/api/saved/save':
                self._send(200, json.dumps(do_save(p), ensure_ascii=False))
            elif u.path == '/api/saved/remove':
                self._send(200, json.dumps(do_unsave(p), ensure_ascii=False))
            elif u.path == '/api/live':
                kw = (p.get('q', [''])[0] or '').strip()
                srcs_sel = [x for x in (p.get('sources', [''])[0] or '').split(',') if x]
                if not kw:
                    self._send(200, json.dumps({'items': [], 'stats': {}}))
                else:
                    srcs = SS.load_sources()
                    loose = (p.get('match', ['exact'])[0] == 'loose')
                    items, stats = SS.run_search(kw, srcs, srcs_sel or ['pansou', 'tg'],
                                                 max_per=60, loose=loose)
                    out = []
                    saved_keys = _saved_url_keys()
                    items.sort(key=lambda x: -score_title(x.get('title', ''), kw, 'loose'))
                    for it in items:
                        out.append({
                            'url': it['url'], 'title': clean_title(it['title']),
                            'raw_title': it['title'], 'disk': it['disk'],
                            'code': it.get('code', ''), 'size_mb': it.get('size_mb'),
                            'share_time': it.get('share_time', ''), 'source': it.get('source', ''),
                            'access': 'unknown', 'probe': None, 'live': True,
                            'saved': _url_key(it['url']) in saved_keys,
                            'dups': 1, 'valid': None,
                        })
                    self._send(200, json.dumps({'items': out, 'stats': stats}, ensure_ascii=False))
            elif u.path == '/api/live/stream':
                kw = (p.get('q', [''])[0] or '').strip()
                sel = [x for x in (p.get('sources', [''])[0] or '').split(',') if x]
                loose = (p.get('match', ['loose'])[0] == 'loose')
                if not kw:
                    self._send(200, 'data: {"done":true}' + chr(10)*2, 'text/event-stream; charset=utf-8')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()
                srcs = SS.load_sources()
                saved_keys = _saved_url_keys()
                seen = set()          # 已出现的 URL
                seen_titles = set()   # 已出现的归一化标题（同课不同分享只留一条）
                # 前端传来的本地结果标题，一并纳入去重
                lt = (p.get('lt', [''])[0] or '')
                if lt:
                    for t in lt.split(chr(1)):
                        tk = _norm_title(clean_title(t))
                        if tk:
                            seen_titles.add(tk)
                pending_persist = []
                try:
                    for key, variant, items, err in SS.stream_search(kw, srcs, sel or ['pansou', 'tg'], loose):
                        fresh = []
                        for it in items:
                            if it['url'] in seen:
                                continue
                            if not SS._relevant(it.get('title', ''), kw):
                                continue
                            # 标题归一化去重：同一门课只保留第一条
                            tk = _norm_title(clean_title(it.get('title', '')))
                            if tk and tk in seen_titles:
                                seen.add(it['url'])
                                continue
                            if tk:
                                seen_titles.add(tk)
                            seen.add(it['url'])
                            fresh.append({
                                'url': it['url'], 'title': clean_title(it['title']),
                                'raw_title': it['title'], 'disk': it['disk'],
                                'code': it.get('code', ''), 'size_mb': it.get('size_mb'),
                                'share_time': it.get('share_time', ''), 'source': it.get('source', ''),
                                'access': 'unknown', 'probe': None, 'live': True,
                                'saved': _url_key(it['url']) in saved_keys,
                                'dups': 1, 'valid': None,
                            })
                        fresh.sort(key=lambda x: -score_title(x['title'], kw))
                        # 先推给浏览器，落库放到流结束后统一做，避免写锁拖慢实时输出
                        if fresh:
                            pending_persist.extend(fresh)
                        payload = json.dumps({'source': key, 'variant': variant,
                                              'items': fresh, 'error': err}, ensure_ascii=False)
                        self.wfile.write(('data: ' + payload + chr(10)*2).encode('utf-8'))
                        self.wfile.flush()
                    self.wfile.write(('data: {"done":true}' + chr(10)*2).encode('utf-8'))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if pending_persist:
                        threading.Thread(target=_save_live_to_items,
                                         args=(pending_persist,), daemon=True).start()
                return
            elif u.path == '/api/sources':
                srcs = SS.load_sources()
                self._send(200, json.dumps({'sources': srcs,
                    'tg_channels': SS.TG_CHANNELS_DEFAULT}, ensure_ascii=False))
            elif u.path == '/api/sources/save':
                raw = p.get('config', [''])[0]
                try:
                    cfg = json.loads(raw)
                    SS.save_sources(cfg)
                    self._send(200, json.dumps({'ok': True}))
                except Exception as e:
                    self._send(400, json.dumps({'error': str(e)[:100]}))
            elif u.path == '/api/tree':
                url = (p.get('url', [''])[0] or '').strip()
                code = (p.get('code', [''])[0] or '').strip()
                if not url:
                    self._send(400, json.dumps({'error': 'url required'}))
                else:
                    r = probe_deep.probe_tree(url, code, max_calls=45, max_depth=2)
                    r['grade'] = probe_deep.grade(r)
                    self._send(200, json.dumps(r, ensure_ascii=False))
            elif u.path == '/api/export':
                data, ctype, n = do_export(p)
                name = (p.get('name', ['export'])[0] or 'export').replace('/', '_')[:60]
                fn = urllib.parse.quote(name + '.csv')
                self._send(200, data, ctype, {'Content-Disposition': f"attachment; filename*=UTF-8''{fn}"})
            else:
                self._send(404, json.dumps({'error': 'not found'}))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._send(500, json.dumps({'error': str(e)[:300]}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8899)
    ap.add_argument('--host', default='0.0.0.0')
    a = ap.parse_args()
    if not os.path.exists(DB):
        print(f'找不到数据库: {DB}'); sys.exit(1)
    srv = ThreadingHTTPServer((a.host, a.port), H)
    print(f'网盘索引库已启动: http://127.0.0.1:{a.port}  (局域网: http://<本机IP>:{a.port})')
    print('按 Ctrl+C 停止')
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')


if __name__ == '__main__':
    main()
