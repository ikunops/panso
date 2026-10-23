# -*- coding: utf-8 -*-
"""fingerprint_v2.py — 通用内容指纹（视频课程 + 文档/素材包都能算）

在 content_dedup 的 video 指纹之外，补充:
  - doc 指纹: 文档/图片类，用文件数 + 总大小 + 文件名集合哈希
  - 判定时: 视频类用 same_course，文档类用 same_pack
"""
import re, hashlib, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import content_dedup as CD

DOC_RE = re.compile(r'\.(pdf|docx?|pptx?|xlsx?|txt|md|epub|mobi|azw3|png|jpe?g|gif|webp|zip|rar|7z)$', re.I)
AD_RE = re.compile(r'(加群|公众号|更多资源|扫码|关注获取|客服微信|QQ群|秒搜鸭|资源说明|获取资源|'
                   r'教程网|进群|加VX|加微信|免费获取|整理不易|www\.|https?://|\.com|\.cn|\.xyz|\.top)', re.I)


def fingerprint(files):
    """返回统一指纹: {'kind':'video'|'doc', ...}"""
    if not files:
        return None
    vids = [f for f in files if CD.VIDEO_RE.search(f['name'])]
    if vids:
        fp = CD.content_fingerprint(files)
        if fp:
            fp['kind'] = 'video'
            fp['file_count'] = len(files)
            promo_files = [f for f in files if AD_RE.search(f['name']) and not CD.VIDEO_RE.search(f['name'])]
            fp['promo'] = len(promo_files)
            # 干净体积 = 扣除推广文件后的总体积（MB），用于质量比较
            all_mb = round(sum(f['size'] for f in files) / 1048576)
            promo_mb = round(sum(f['size'] for f in promo_files) / 1048576)
            fp['total_mb'] = all_mb
            fp['clean_mb'] = max(0, all_mb - promo_mb)
            return fp
    # 文档/素材类
    docs = [f for f in files if DOC_RE.search(f['name'])]
    if not docs:
        return None
    names = sorted(re.sub(r'^[\d.\-_、）)]+\s*', '', f['name']) for f in docs)
    total = sum(f['size'] for f in docs)
    h = hashlib.md5('|'.join(names).encode('utf-8')).hexdigest()[:16]
    return {
        'kind': 'doc',
        'doc_count': len(docs),
        'doc_mb': round(total / 1048576),
        'name_hash': h,
        'names': [n[:30] for n in names[:60]],
        'file_count': len(files),
        'promo': sum(1 for f in files if AD_RE.search(f['name'])),
        'total_mb': round(sum(f['size'] for f in files) / 1048576),
        'clean_mb': round(sum(f['size'] for f in files
                              if not AD_RE.search(f['name'])) / 1048576),
    }


# 门槛：只有"成体系资源包"才参与内容判重
# 单文件/小包（一个 APK、一张专辑、一本书）结构太相似，靠内容判重必然误杀
MIN_DOC_COUNT = 8          # 文档类至少 8 个文件
MIN_DOC_MB = 100           # 且至少 100MB
MIN_VIDEO_COUNT = 3        # 视频类至少 3 个视频
MIN_NAME_LEN = 6           # 文件名太短(如 "1.pdf")不参与比对


def _name_signal(fp):
    """文件名信息量：去掉纯数字/纯序号后的平均长度。
    短剧/影视的文件名多为 01.mp4、02.mp4，信息量极低，不能靠它判重。"""
    names = fp.get('video_names') if fp.get('kind') == 'video' else fp.get('names')
    if not names:
        return 0
    stripped = []
    for n in names:
        t = re.sub(r'^[\d\s.\-_、）)]+', '', n)
        t = re.sub(r'^第?\d+[集讲节课期章]', '', t)
        stripped.append(t)
    if not stripped:
        return 0
    return sum(len(x) for x in stripped) / len(stripped)


def _eligible(fp):
    """该指纹是否够格参与内容判重"""
    if not fp:
        return False
    k = fp.get('kind')
    if k == 'video':
        if (fp.get('video_count') or 0) < MIN_VIDEO_COUNT:
            return False
        # 文件名平均信息量太低（全是 01.mp4 这类）→ 不参与判重
        return _name_signal(fp) >= 4
    if k == 'doc':
        return (fp.get('doc_count') or 0) >= MIN_DOC_COUNT and (fp.get('doc_mb') or 0) >= MIN_DOC_MB
    return False


def same_pack(fp1, fp2):
    """文档类判重：文件名集合重合率高 + 体积接近（仅限成体系资源包）"""
    if not fp1 or not fp2:
        return False
    if fp1.get('kind') != 'doc' or fp2.get('kind') != 'doc':
        return False
    if not _eligible(fp1) or not _eligible(fp2):
        return False
    # 过滤掉过短的文件名（"1.pdf"、"封面.jpg" 这类无区分度）
    a = {n for n in (fp1.get('names') or []) if len(n) >= MIN_NAME_LEN}
    b = {n for n in (fp2.get('names') or []) if len(n) >= MIN_NAME_LEN}
    if len(a) < 3 or len(b) < 3:
        return False
    overlap = len(a & b) / max(len(a), len(b))
    if overlap < 0.7:
        return False
    m1, m2 = fp1.get('doc_mb') or 0, fp2.get('doc_mb') or 0
    if m1 and m2 and abs(m1 - m2) / max(m1, m2) > 0.05:
        return False
    return True


def pick_best(group):
    """漏斗式择优：从同款组里挑最该保留的那份

    漏斗顺序（前一层分出胜负就不再往下比）:
      1. 有效优先（dead 直接淘汰）
      2. 干净体积大者优先 —— 扣除推广文件后的体积，代表真实内容量
         （体积大但推广也多的，干净体积会被扣掉，自然排后面）
      3. 推广文件少者优先 —— 干净体积相同时，少带垃圾的更干净
      4. 有提取码者优先
      5. 收录时间新者优先

    参数: group = [{'fp':..., 'access':..., 'code':..., 'at':...}, ...]
    返回: 排序后的列表，第 0 个即应保留的
    """
    RANK = {'accessible': 0, 'with_code': 1, 'unknown': 2, 'dead': 3}

    def key(x):
        fp = x.get('fp') or {}
        clean = fp.get('clean_mb') or 0
        # 视频类用 clean_mb；文档类用 doc_mb（本身不含推广，因为只统计 doc 扩展名）
        if fp.get('kind') == 'doc':
            clean = fp.get('doc_mb') or clean
        return (
            RANK.get(x.get('access'), 2),      # 1. 有效性
            -clean,                            # 2. 干净体积降序
            fp.get('promo', 0),                # 3. 推广文件数升序
            0 if x.get('code') else 1,         # 4. 有码优先
            x.get('at') or '',                 # 5. 时间新（字符串比较，逆序见下）
        )

    # at 需要逆序，单独处理
    ordered = sorted(group, key=lambda x: (
        RANK.get(x.get('access'), 2),
        -((x.get('fp') or {}).get('clean_mb') or (x.get('fp') or {}).get('doc_mb') or 0),
        (x.get('fp') or {}).get('promo', 0),
        0 if x.get('code') else 1,
    ))
    return ordered


def title_similar(t1, t2):
    """标题相似度（0~1）：判断是否同一主题
    用字符集合 Jaccard + 最长公共子串长度双重判定"""
    if not t1 or not t2:
        return 0.0
    import unicodedata
    def clean(x):
        return ''.join(ch for ch in x.lower()
                       if not ch.isspace() and not unicodedata.category(ch).startswith(('P', 'S')))
    a, b = clean(t1), clean(t2)
    if not a or not b:
        return 0.0
    # 字符集合 Jaccard
    sa, sb = set(a), set(b)
    jac = len(sa & sb) / max(len(sa | sb), 1)
    # 最长公共子串占比
    def lcs_len(x, y):
        m = [[0] * (len(y) + 1) for _ in range(len(x) + 1)]
        best = 0
        for i in range(1, len(x) + 1):
            for j in range(1, len(y) + 1):
                if x[i-1] == y[j-1]:
                    m[i][j] = m[i-1][j-1] + 1
                    best = max(best, m[i][j])
        return best
    lcs = lcs_len(a[:80], b[:80]) / max(min(len(a), len(b)), 1)
    return max(jac, lcs)


MIN_TITLE_SIM = 0.35   # 标题相似度下限：低于此值即使内容指纹像也不判重


def same_content(fp1, fp2, title1='', title2=''):
    """统一判重入口（准入门槛 + 标题相似度校验）"""
    if not fp1 or not fp2:
        return False
    if not _eligible(fp1) or not _eligible(fp2):
        return False
    k1, k2 = fp1.get('kind'), fp2.get('kind')
    if k1 != k2:
        return False
    # 内容指纹匹配
    if k1 == 'video':
        matched = CD.same_course(fp1, fp2)
    else:
        matched = same_pack(fp1, fp2)
    if not matched:
        return False
    # 标题必须也相关（防止"幼儿教育"和"小学记叙文"这类误合）
    if title1 and title2:
        sim = title_similar(title1, title2)
        if sim < MIN_TITLE_SIM:
            return False
    return True


if __name__ == '__main__':
    import json
    cache = json.load(open(sys.argv[1] if len(sys.argv) > 1 else 'content_fp_saved.json', encoding='utf-8'))
    ok = sum(1 for v in cache.values() if v)
    vid = sum(1 for v in cache.values() if v and v.get('kind') == 'video')
    doc = sum(1 for v in cache.values() if v and v.get('kind') == 'doc')
    print(f'指纹总数 {len(cache)} | 有效 {ok} (视频 {vid} / 文档 {doc}) | 无效 {len(cache)-ok}')
