# -*- coding: utf-8 -*-
"""content_probe.py — 网盘分享内容体检
对夸克/百度链接探测真实目录结构，返回文件数/总大小/结构样例，用于判断内容完整度。
用法:
    python content_probe.py <url> [code]        单条探测
    python content_probe.py --from-list 课程网盘资源清单.txt   批量(可加 --limit N)
"""
import re, json, time, sys, argparse
import httpx

UA_PC = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
UA_M = 'Mozilla/5.0 (Linux; Android 10; SM-G975F) AppleWebKit/537.36 Chrome/87.0.4280.101 Mobile Safari/537.36'


# ---------------- 夸克 ----------------
def quark_probe(pid, passcode=''):
    """返回 {'ok':bool, 'files':int, 'dirs':int, 'size':int, 'top':[...], 'tree':[...]}"""
    out = {'ok': False, 'files': 0, 'dirs': 0, 'size': 0, 'top': [], 'tree': []}
    try:
        with httpx.Client(timeout=20, headers={'User-Agent': UA_PC, 'Referer': 'https://pan.quark.cn/'}) as c:
            t = c.post('https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc',
                       json={'pwd_id': pid, 'passcode': passcode})
            j = t.json()
            st = (j.get('data') or {}).get('stoken')
            if not st:
                out['err'] = 'no-stoken:' + str(j.get('message', ''))[:40]
                return out
            def detail(pdir='0', size=100):
                r = c.get('https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail', params={
                    'pr': 'ucpro', 'fr': 'pc', 'pwd_id': pid, 'stoken': st,
                    'pdir_fid': pdir, 'force': '0', '_page': '1', '_size': str(size),
                })
                dj = r.json()
                return (dj.get('data') or {}).get('list') or []
            top = detail()
            for x in top:
                isdir = bool(x.get('dir'))
                ent = {'name': x.get('file_name', ''), 'isdir': isdir, 'size': int(x.get('size') or 0)}
                out['top'].append(ent)
                if isdir:
                    out['dirs'] += 1
                else:
                    out['files'] += 1; out['size'] += ent['size']
            # 展开第一层目录（最多 3 个）
            for ent in [e for e in out['top'] if e['isdir']][:3]:
                fid = None
                for x in top:
                    if x.get('file_name') == ent['name']:
                        fid = x.get('fid'); break
                if not fid:
                    continue
                kids = detail(fid, 60)
                nf = sum(1 for k in kids if not k.get('dir'))
                nd = sum(1 for k in kids if k.get('dir'))
                sz = sum(int(k.get('size') or 0) for k in kids if not k.get('dir'))
                out['files'] += nf; out['dirs'] += nd; out['size'] += sz
                out['tree'].append({'dir': ent['name'], 'items': len(kids), 'files': nf, 'dirs': nd,
                                    'size': sz, 'sample': [('D ' if k.get('dir') else 'F ') + (k.get('file_name') or '')[:60] for k in kids[:8]]})
            out['ok'] = True
            return out
    except Exception as e:
        out['err'] = 'exc:' + str(e)[:60]
        return out


# ---------------- 百度 ----------------
_NAME_RE = re.compile(r'"server_filename":"((?:[^"\\]|\\.)*)"')

def _bd_page_filelist(page):
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
            if nxt == start: break
            start = nxt; tries += 1
    return None

def _bd_listdir(c, uk, shareid, path, referer, max_pages=3):
    out, errno = [], None
    for p in range(1, max_pages + 1):
        try:
            r = c.get('https://pan.baidu.com/share/list', params={
                'uk': uk, 'shareid': shareid, 'order': 'other', 'desc': '1', 'showempty': '0',
                'web': '1', 'page': p, 'num': 100, 'dir': path,
                't': str(int(time.time() * 1000)), 'channel': 'chunlei', 'app_id': '250528', 'clienttype': '0',
            }, headers={'User-Agent': UA_PC, 'Referer': referer}, timeout=25)
            j = r.json()
            errno = j.get('errno')
            if errno != 0: break
            lst = j.get('list') or []
            out += [{'name': x.get('server_filename', ''), 'isdir': 1 if x.get('isdir') else 0,
                     'size': int(x.get('size') or 0), 'path': x.get('path', '')} for x in lst]
            if len(lst) < 100: break
        except Exception as e:
            errno = 'exc'; break
        time.sleep(0.3)
    return out, errno

def baidu_probe(short, code=''):
    """short 为 /s/ 后的整段；返回结构同夸克"""
    out = {'ok': False, 'files': 0, 'dirs': 0, 'size': 0, 'top': [], 'tree': []}
    try:
        with httpx.Client(follow_redirects=True) as c:
            url = f'https://pan.baidu.com/s/{short}'
            c.get(url, headers={'User-Agent': UA_PC, 'Referer': 'https://pan.baidu.com/disk/home'}, timeout=20)
            if code:
                surl = short[1:] if short.startswith('1') else short
                c.post('https://pan.baidu.com/share/verify', params={
                    'surl': surl, 't': str(int(time.time() * 1000)), 'channel': 'chunlei',
                    'web': '1', 'app_id': '250528', 'clienttype': '0',
                }, data={'pwd': code, 'vcode': '', 'vcode_str': ''},
                    headers={'User-Agent': UA_PC, 'Referer': url}, timeout=20)
            r = c.get(url, headers={'User-Agent': UA_PC, 'Referer': url}, timeout=20)
            page = r.text
            m1 = re.search(r'shareid\s*:\s*"?(\d+)', page); m2 = re.search(r'share_uk\s*:\s*"?(\d+)', page)
            sid, uk = (m1.group(1) if m1 else None), (m2.group(1) if m2 else None)
            blk = _bd_page_filelist(page)
            # 页面异步渲染时可能读不到列表，重试
            for _ in range(2):
                if blk: break
                time.sleep(1.5)
                r = c.get(url, headers={'User-Agent': UA_PC, 'Referer': url}, timeout=20)
                page = r.text
                if not sid:
                    m1 = re.search(r'shareid\s*:\s*"?(\d+)', page)
                    sid = m1.group(1) if m1 else None
                if not uk:
                    m2 = re.search(r'share_uk\s*:\s*"?(\d+)', page)
                    uk = m2.group(1) if m2 else None
                blk = _bd_page_filelist(page)
            roots = []
            if blk:
                roots = [{'name': x.get('server_filename', ''), 'isdir': 1 if x.get('isdir') else 0,
                          'size': int(x.get('size') or 0), 'path': x.get('path', '')} for x in (blk.get('file_list') or [])]
            if not roots:
                out['err'] = f'no-list(sid={sid},uk={uk})'
                return out
            out['top'] = roots
            for x in roots:
                if x['isdir']: out['dirs'] += 1
                else: out['files'] += 1; out['size'] += x['size']
            if sid and uk:
                for d in [x for x in roots if x['isdir']][:2]:
                    l1, err = _bd_listdir(c, uk, sid, d['path'] or '/', url)
                    if err != 0:
                        out['tree'].append({'dir': d['name'], 'errno': err, 'items': 0, 'files': 0, 'dirs': 0, 'size': 0, 'sample': []})
                        continue
                    nf = sum(1 for k in l1 if not k['isdir']); nd = sum(1 for k in l1 if k['isdir'])
                    sz = sum(k['size'] for k in l1 if not k['isdir'])
                    out['files'] += nf; out['dirs'] += nd; out['size'] += sz
                    out['tree'].append({'dir': d['name'], 'items': len(l1), 'files': nf, 'dirs': nd, 'size': sz,
                                        'sample': [('D ' if k['isdir'] else 'F ') + k['name'][:60] for k in l1[:8]]})
                    time.sleep(0.35)
            out['ok'] = True
            return out
    except Exception as e:
        out['err'] = 'exc:' + str(e)[:60]
        return out


# ---------------- 统一入口 ----------------
def probe(url, code=''):
    if 'pan.quark.cn/s/' in url:
        pid = url.rstrip('/').rsplit('/', 1)[1].split('?')[0]
        r = quark_probe(pid, code)
        r['disk'] = '夸克'
    elif 'pan.baidu.com/s/' in url:
        short = url.split('/s/')[1].split('?')[0]
        r = baidu_probe(short, code)
        r['disk'] = '百度'
    else:
        # 阿里/其他暂不支持目录探测
        return {'ok': False, 'err': 'unsupported-disk', 'disk': '?'}
    r['url'] = url
    return r

# 分卷压缩特征: 1.zip+1.z01/z02, x.part01.rar, x.rar.001, x.7z.001
SPLIT_RE = re.compile(r'(\.z\d{2}$|\.part\d{1,3}\.(rar|zip|7z)$|\.(rar|zip|7z)\.\d{3}$|\.r\d{2}$)', re.I)
# 编码分段特征: 命名以 001/0001 开头或结尾 (如 001-xxx.mp4)
SEG_RE = re.compile(r'(^|[^0-9])(\d{3,4})[-_ ]')
# 标题/目录名里的区间片段: 13-24篇, 第1-12讲, 6-10章
RANGE_RE = re.compile(r'(\d{1,3})\s*[-~到至]\s*(\d{1,3})\s*(篇|讲|章|节|部|集|课|期)')

def _split_info(top):
    """检测根目录是否为分卷压缩"""
    names = [x['name'] for x in top if not x['isdir']]
    if not names: return None
    parts = [n for n in names if SPLIT_RE.search(n)]
    if parts and len(parts) >= 2:
        base = re.sub(SPLIT_RE, '', parts[0])
        same_base = all(re.sub(SPLIT_RE, '', p) == base for p in parts)
        return {'count': len(parts), 'base': base, 'same_base': same_base}
    return None

def verdict(r, title=''):
    """按结构给完整度判定; title 可选用于区间片段检测"""
    if not r.get('ok'):
        return '❓无法读取(' + str(r.get('err', ''))[:24] + ')'
    nf, nd, sz = r.get('files', 0), r.get('dirs', 0), r.get('size', 0)
    top = r.get('top') or []
    tags = []
    # 分卷检测
    sp = _split_info(top)
    if sp:
        tags.append(f'分卷x{sp["count"]}({sp["base"][:20]})')
    # 区间片段检测（标题或根名）
    cand_txt = (title or '') + ' ' + ' '.join(x['name'] for x in top[:3])
    rm = RANGE_RE.search(cand_txt)
    if rm:
        tags.append(f'区间{rm.group(1)}-{rm.group(2)}{rm.group(3)}')
    # 单文件小分享（电子书/单资料）
    if nf <= 2 and nd == 0 and not tags:
        return f'📄单文件({sz/1048576:.0f}MB)'
    # 碎片判定: 有分卷或区间标记 且 结构单薄
    if (sp or rm) and nd <= 1 and nf <= 8:
        return f'⚠️疑似碎片({"、".join(tags)}; {nf}文件/{nd}目录, {sz/1048576:.0f}MB)'
    if tags:
        return f'🟡含标记({"、".join(tags)}; {nf}文件/{nd}目录, {sz/1048576:.0f}MB)'
    if nd >= 5 or nf >= 20 or sz > 2 * 1024 * 1048576:
        return f'✅结构完整({nf}文件/{nd}目录, {sz/1048576:.0f}MB)'
    if nd >= 1 and nf >= 5:
        return f'🟡结构可用({nf}文件/{nd}目录, {sz/1048576:.0f}MB)'
    return f'⚠️疑似碎片({nf}文件/{nd}目录, {sz/1048576:.0f}MB)'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('url', nargs='?')
    ap.add_argument('code', nargs='?', default='')
    ap.add_argument('--from-list', help='从清单文件读取(TSV: 名称/网盘/链接/提取码)')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--out', default='content_probe.json')
    a = ap.parse_args()

    if a.from_list:
        rows = []
        for line in open(a.from_list, encoding='utf-8'):
            p = line.rstrip('\n').split('\t')
            if len(p) != 4 or p[0] == '名称': continue
            name, disk, url, code = p
            if 'quark' in url or 'baidu' in url:
                rows.append((name, url, code if re.fullmatch(r'[0-9a-zA-Z]{4}', code or '') else ''))
        if a.limit: rows = rows[:a.limit]
        result = {}
        for i, (name, url, code) in enumerate(rows, 1):
            r = probe(url, code)
            result[url] = {'name': name, **r}
            print(f"[{i}/{len(rows)}] {verdict(r, name)} | {name[:40]}", flush=True)
            json.dump(result, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
            time.sleep(0.4)
        print(f'完成 -> {a.out}')
    elif a.url:
        r = probe(a.url, a.code)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        print('判定:', verdict(r))
    else:
        ap.print_help()

if __name__ == '__main__':
    main()
