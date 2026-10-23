# -*- coding: utf-8 -*-
"""probe_deep.py — 深度内容探测 + 可获取性判定（整合 ai_quality_check 的递归逻辑）

对外提供:
    probe_tree(url, code, max_calls) -> dict   完整目录树（供 Web 展开预览）
    probe_quality(url, code)         -> dict   统计 + 质量分级
    access_state(row)                -> str    可获取性判定
"""
import re, time, json, os, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UA_PC = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
VIDEO_RE = re.compile(r'\.(mp4|mkv|avi|mov|flv|wmv|m4v|ts|rmvb|webm)$', re.I)
DOC_RE = re.compile(r'\.(pdf|docx?|pptx?|xlsx?|txt|md|epub|mobi|azw3)$', re.I)
ARCHIVE_RE = re.compile(r'\.(zip|rar|7z|tar|gz|iso)$', re.I)


# ============ 夸克：BFS 递归 ============
def quark_tree(pid, passcode='', max_calls=60, page_size=100, max_depth=3, time_budget=25):
    """返回 {ok, need_code, files, dirs, videos, size, tree, truncated, err}
    tree 为嵌套结构，供前端展开显示（受 max_calls/max_depth 限制）"""
    out = {'ok': False, 'need_code': False, 'files': 0, 'dirs': 0, 'videos': 0,
           'docs': 0, 'archives': 0, 'size': 0, 'tree': [], 'truncated': False, 'err': ''}
    t0 = time.time()
    try:
        with httpx.Client(timeout=20, headers={'User-Agent': UA_PC, 'Referer': 'https://pan.quark.cn/'}) as c:
            j = c.post('https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc',
                       json={'pwd_id': pid, 'passcode': passcode}).json()
            st = (j.get('data') or {}).get('stoken')
            if not st:
                msg = str(j.get('message', ''))
                if '提取码' in msg or '密码' in msg:
                    out['need_code'] = True
                    out['err'] = 'need-code'
                else:
                    out['err'] = 'no-stoken:' + msg[:30]
                return out

            def ls(fid):
                r = c.get('https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail', params={
                    'pr': 'ucpro', 'fr': 'pc', 'pwd_id': pid, 'stoken': st, 'pdir_fid': fid,
                    'force': '0', '_page': '1', '_size': str(page_size)})
                d = (r.json().get('data') or {})
                return d.get('list') or []

            calls = 0
            root_items = ls('0')
            calls += 1

            def walk(items, depth):
                nonlocal calls
                nodes = []
                for x in items:
                    nm = x.get('file_name') or ''
                    isdir = bool(x.get('dir'))
                    node = {'name': nm, 'dir': isdir, 'size': int(x.get('size') or 0)}
                    if isdir:
                        out['dirs'] += 1
                        node['children'] = []
                        if depth < max_depth and calls < max_calls and (time.time() - t0) < time_budget:
                            calls += 1
                            try:
                                kids = ls(x.get('fid'))
                                node['children'] = walk(kids, depth + 1)
                                node['count'] = len(kids)
                            except Exception:
                                node['error'] = True
                        else:
                            out['truncated'] = True
                    else:
                        out['files'] += 1
                        out['size'] += node['size']
                        if VIDEO_RE.search(nm):
                            out['videos'] += 1
                        elif DOC_RE.search(nm):
                            out['docs'] += 1
                        elif ARCHIVE_RE.search(nm):
                            out['archives'] += 1
                    nodes.append(node)
                return nodes

            out['tree'] = walk(root_items, 0)
            out['ok'] = True
            return out
    except Exception as e:
        out['err'] = 'exc:' + str(e)[:60]
        return out


# ============ 百度：verify + 两层 ============
def _bd_ids(page):
    sid = re.search(r'shareid\s*:\s*"?(\d+)', page)
    uk = re.search(r'share_uk\s*:\s*"?(\d+)', page)
    return (sid.group(1) if sid else None), (uk.group(1) if uk else None)


def _bd_filelist(page):
    dec = json.JSONDecoder()
    for m in re.finditer(r'"file_list"', page):
        pos = m.start()
        start = page.rfind('{', 0, pos)
        tries = 0
        while start > 0 and tries < 40:
            try:
                obj, _ = dec.raw_decode(page[start:])
                if isinstance(obj, dict) and 'file_list' in obj:
                    return obj
            except Exception:
                pass
            nxt = page.rfind('{', 0, start)
            if nxt == start:
                break
            start = nxt
            tries += 1
    return None


def baidu_tree(short, code='', max_calls=25, max_depth=2, time_budget=25):
    out = {'ok': False, 'need_code': False, 'files': 0, 'dirs': 0, 'videos': 0,
           'docs': 0, 'archives': 0, 'size': 0, 'tree': [], 'truncated': False, 'err': ''}
    t0 = time.time()
    try:
        with httpx.Client(follow_redirects=True, timeout=20) as c:
            url = f'https://pan.baidu.com/s/{short}'
            c.get(url, headers={'User-Agent': UA_PC, 'Referer': 'https://pan.baidu.com/disk/home'})
            if code:
                surl = short[1:] if short.startswith('1') else short
                try:
                    c.post('https://pan.baidu.com/share/verify',
                           params={'surl': surl, 't': str(int(time.time() * 1000)), 'channel': 'chunlei',
                                   'web': '1', 'app_id': '250528', 'clienttype': '0'},
                           data={'pwd': code, 'vcode': '', 'vcode_str': ''},
                           headers={'User-Agent': UA_PC, 'Referer': url})
                except Exception:
                    pass
            r = c.get(url, headers={'User-Agent': UA_PC, 'Referer': url})
            page = r.text
            blk = _bd_filelist(page)
            if not blk:
                # 判定是否需要码
                if '请输入提取码' in page or '提取文件' in page:
                    out['need_code'] = not bool(code)
                    out['err'] = 'need-code'
                else:
                    out['err'] = 'no-list'
                return out
            sid, uk = _bd_ids(page)
            calls = 0

            def ls(path):
                nonlocal calls
                calls += 1
                try:
                    j = c.get('https://pan.baidu.com/share/list', params={
                        'uk': uk, 'shareid': sid, 'order': 'other', 'desc': '1', 'showempty': '0',
                        'web': '1', 'page': 1, 'num': 100, 'dir': path,
                        't': str(int(time.time() * 1000)), 'channel': 'chunlei',
                        'app_id': '250528', 'clienttype': '0'},
                        headers={'User-Agent': UA_PC, 'Referer': url}, timeout=20).json()
                    if j.get('errno') != 0:
                        return None
                    return [{'name': x.get('server_filename', ''), 'dir': 1 if x.get('isdir') else 0,
                             'size': int(x.get('size') or 0), 'path': x.get('path', '')} for x in (j.get('list') or [])]
                except Exception:
                    return None

            def walk(items, depth):
                nonlocal calls
                nodes = []
                for x in items:
                    nm = x['name']
                    node = {'name': nm, 'dir': bool(x['dir']), 'size': x['size']}
                    if x['dir']:
                        out['dirs'] += 1
                        node['children'] = []
                        if depth < max_depth and calls < max_calls and (time.time() - t0) < time_budget:
                            kids = ls(x['path'])
                            if kids is None:
                                node['error'] = True
                            else:
                                node['children'] = walk(kids, depth + 1)
                                node['count'] = len(kids)
                        else:
                            out['truncated'] = True
                    else:
                        out['files'] += 1
                        out['size'] += node['size']
                        if VIDEO_RE.search(nm):
                            out['videos'] += 1
                        elif DOC_RE.search(nm):
                            out['docs'] += 1
                        elif ARCHIVE_RE.search(nm):
                            out['archives'] += 1
                    nodes.append(node)
                return nodes

            root = [{'name': x.get('server_filename', ''), 'dir': 1 if x.get('isdir') else 0,
                     'size': int(x.get('size') or 0), 'path': x.get('path', '')}
                    for x in (blk.get('file_list') or [])]
            out['tree'] = walk(root, 0)
            out['ok'] = True
            return out
    except Exception as e:
        out['err'] = 'exc:' + str(e)[:60]
        return out


# ============ 统一入口 ============
def probe_tree(url, code='', max_calls=60, max_depth=3):
    if 'pan.quark.cn/s/' in url:
        pid = url.rstrip('/').rsplit('/', 1)[1].split('?')[0]
        return quark_tree(pid, code, max_calls=max_calls, max_depth=max_depth)
    if 'pan.baidu.com/s/' in url:
        short = url.split('/s/')[1].split('?')[0]
        return baidu_tree(short, code, max_calls=min(max_calls, 25), max_depth=max_depth)
    return {'ok': False, 'err': 'unsupported-disk', 'need_code': False, 'tree': []}


def grade(q):
    """质量分级：视频数是核心信号"""
    if not q.get('ok'):
        if q.get('need_code'):
            return '🔒需要提取码'
        return '❓无法读取'
    nf, nd, sz, vd = q.get('files', 0), q.get('dirs', 0), q.get('size', 0), q.get('videos', 0)
    gb = sz / 1073741824
    if vd >= 20 or (vd >= 10 and gb >= 5):
        return f'🏆优秀({nf}文件/视频{vd}, {gb:.1f}GB)'
    if vd >= 5 or gb >= 2 or nf >= 30:
        return f'✅良好({nf}文件/视频{vd}, {gb:.1f}GB)'
    if vd >= 1 or gb >= 0.3 or nf >= 8:
        return f'🟡一般({nf}文件/视频{vd}, {gb:.2f}GB)'
    if nf == 0 and nd == 0:
        return '🕳️空壳'
    if nf <= 3 and nd == 0:
        return f'📄单文件({gb*1024:.0f}MB)'
    return f'⚠️偏少({nf}文件/{nd}目录, {gb:.2f}GB)'


def access_state(valid, need_code, code):
    """可获取性判定：这才是用户真正关心的
    valid: 1/0/None   need_code: True/False/None   code: 提取码字符串
    """
    if valid == 0:
        return 'dead'          # 链接已失效
    if valid is None:
        return 'unknown'       # 未验活
    # valid == 1：分享存活
    if need_code is True:
        return 'coded' if code else 'missing_code'
    if need_code is False:
        return 'direct'
    return 'alive_unk'         # 存活但码状态未知


ACCESS_LABEL = {
    'direct': '✅可直接获取',
    'coded': '🔑凭码可获取',
    'missing_code': '🔒缺码',
    'alive_unk': '❔存活待确认',
    'dead': '❌已失效',
    'unknown': '⚪未验证',
}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('code', nargs='?', default='')
    a = ap.parse_args()
    r = probe_tree(a.url, a.code)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:3000])
    print('\n分级:', grade(r))
