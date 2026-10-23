# -*- coding: utf-8 -*-
"""内容级去重：抓取每个分享的【视频文件清单】，以视频名序号集合+总大小做指纹
同一门课不同分享（多了广告文件/改名）会被识别为重复，保留最干净的那份。
"""
import re, sys, time, json
import httpx

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
VIDEO_RE = re.compile(r'\.(mp4|mkv|avi|mov|flv|wmv|m4v|ts)$', re.I)
# 课程编号: 01. / 3、/ Day12 / 第5讲
NUM_RE = re.compile(r'^(?:day|d)?\s*[\[\(]?(\d{1,3})[\].、\-_ ]', re.I)


def quark_files(url, passcode='', max_dir_calls=200, page=200):
    """返回所有文件的 (相对路径, 名称, 大小)，递归到底"""
    pid = url.rstrip('/').rsplit('/', 1)[1].split('?')[0]
    files, dirs_seen = [], set()
    with httpx.Client(timeout=25, headers={'User-Agent': UA, 'Referer': 'https://pan.quark.cn/'}) as c:
        j = c.post('https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc',
                   json={'pwd_id': pid, 'passcode': passcode}).json()
        st = (j.get('data') or {}).get('stoken')
        if not st:
            return None
        stack = [('0', '')]
        calls = 0
        while stack and calls < max_dir_calls:
            fid, prefix = stack.pop()
            r = c.get('https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail', params={
                'pr': 'ucpro', 'fr': 'pc', 'pwd_id': pid, 'stoken': st, 'pdir_fid': fid,
                'force': '0', '_page': '1', '_size': str(page)})
            calls += 1
            items = (r.json().get('data') or {}).get('list') or []
            for x in items:
                nm = x.get('file_name') or ''
                if x.get('dir'):
                    if x.get('fid') and x['fid'] not in dirs_seen:
                        dirs_seen.add(x['fid'])
                        stack.append((x['fid'], prefix + '/' + nm))
                else:
                    files.append({'name': nm, 'size': int(x.get('size') or 0), 'path': prefix + '/' + nm})
            time.sleep(0.08)
    return files


def content_fingerprint(files):
    """基于视频文件构造内容指纹"""
    if not files: return None
    vids = [f for f in files if VIDEO_RE.search(f['name'])]
    if not vids: return None
    # 用视频总体积(取整MB) + 视频数量 + 归一化后的编号集合
    total = sum(v['size'] for v in vids)
    nums = set()
    for v in vids:
        base = re.sub(VIDEO_RE, '', v['name'])
        base = re.sub(r'^[\d]+[.\-_、）)]?\s*', '', base)
        m = NUM_RE.search(v['name'])
        if m: nums.add(int(m.group(1)))
        else:
            m2 = re.search(r'[.\-_、](\d{1,3})[.\-_]', v['name'])
            if m2: nums.add(int(m2.group(1)))
    return {'video_count': len(vids), 'video_mb': round(total / 1048576),
            'nums': sorted(nums), 'video_names': sorted(
                [re.sub(r'^[\d.\-_]+', '', re.sub(VIDEO_RE, '', v['name']))[:20] for v in vids])}


def same_course(fp1, fp2):
    if not fp1 or not fp2: return False
    # 视频数相同 + 体积差<3% + 编号集合重合>80%
    if abs(fp1['video_count'] - fp2['video_count']) > 1: return False
    if fp1['video_mb'] and fp2['video_mb']:
        if abs(fp1['video_mb'] - fp2['video_mb']) / max(fp1['video_mb'], fp2['video_mb']) > 0.03:
            return False
    if fp1['nums'] and fp2['nums']:
        a, b = set(fp1['nums']), set(fp2['nums'])
        if len(a & b) / max(len(a), len(b)) < 0.8: return False
    # 视频名重合率
    if fp1['video_names'] and fp2['video_names']:
        a, b = set(fp1['video_names']), set(fp2['video_names'])
        if len(a & b) / max(len(a), len(b)) < 0.7: return False
    return True


if __name__ == '__main__':
    urls = sys.argv[1:]
    fprints = {}
    for u in urls:
        fs = quark_files(u)
        fp = content_fingerprint(fs) if fs else None
        fprints[u] = fp
        if fp:
            print(f"{u}\n   视频{fp['video_count']}  {fp['video_mb']}MB  编号{fp['nums'][:12]}")
    print()
    keys = list(fprints)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            print(f"{'相同' if same_course(fprints[keys[i]], fprints[keys[j]]) else '不同'}  {keys[i][-12:]} vs {keys[j][-12:]}")
