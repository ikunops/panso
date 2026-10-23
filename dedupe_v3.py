# -*- coding: utf-8 -*-
"""dedupe_v3.py — 保守的重复识别（只合并真正的同课多分享）

判定（必须同时满足）:
  1) 归一化后，短标题是长标题的子串 —— 保证是同一主体
  2) 被吃掉的部分（差集）只含允许的修饰词，不含数字/不同关键词
     允许: 完整版 全套 完结 更新 先保存 避免丢失 内部 合集 教程 课程 加我 微信 公众号 等
     禁止: 出现不同的数字(如日期/期数)、不同的专有名词(北大/清华)
  3) 或者归一化后完全相同（只差标点/序号）

保留策略: 有效 > 有码 > 有体积 > 标题更长 > 时间新
"""
import sqlite3, re, sys, unicodedata

DB = r'H:\zcode-workspace\pan_index.db'
APPLY = '--apply' in sys.argv

ALLOW_FILLER = [
    '完整版', '全套', '完结', '更新中', '持续更新', '最新', '最新版', '高清', '免费', '永久',
    '先保存后观看', '避免丢失', '内部', '资料', '合集', '教程', '课程', '视频', '带源码',
    '带源码课件', '含源码', '完整', '全', '版', '个', '套', '节', '课', '集', '讲', '章',
    '先保存', '后观看', '避免', '丢失', '请', '收藏', '整理', '超全', '精讲', '最新',
    '课', '程', '教', '学', '习', '资', '料',
]
FILLER_SET = set()
for w in ALLOW_FILLER:
    for ch in w:
        FILLER_SET.add(ch)

DIGIT = re.compile(r'\d')

SUFFIX = re.compile(r'(\(?\d+\)?|（\d+）|副本|copy)$', re.I)


def norm(t):
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
    for _ in range(3):
        s2 = SUFFIX.sub('', s)
        if s2 == s:
            break
        s = s2
    return s.lower()


def is_dup_pair(a, b):
    """判断两个归一化标题是否同一内容"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) < 8 or short not in long_:
        return False
    extra = long_.replace(short, '', 1)
    if not extra:
        return True
    # 数字若作为"规格标注"（50节/17周/2.5GB）则允许；否则视为不同资源（如 240926 vs 240924）
    SPEC = re.compile(r'\d+(?:\.\d+)?\s*(?:节|集|讲|课|章|部|周|天|小时|分钟|G|GB|MB|KB|个|套|册|本|篇|期)?')
    # 先把"数字+规格单位"剔除，再看有没有裸数字
    extra_no_spec = re.sub(r'\d+(?:\.\d+)?(?:节|集|讲|课|章|部|周|天|小时|分钟|G|GB|MB|KB|个|套|册|本|篇)', '', extra)
    if DIGIT.search(extra_no_spec):
        return False
    # 差集里的字符必须在允许集合里（排除 北大/清华 这类不同专名）
    bad = [ch for ch in extra if ch not in FILLER_SET]
    return len(bad) <= 1


con = sqlite3.connect(DB, timeout=30)
cur = con.cursor()
cur.execute('PRAGMA journal_mode=WAL')

rows = cur.execute('SELECT url,title,code,size_mb,access,saved_at FROM saved').fetchall()
items = [{'url': r[0], 'title': r[1], 'code': r[2], 'size': r[3],
          'access': r[4], 'at': r[5], 'key': norm(r[1])} for r in rows]
items.sort(key=lambda x: -len(x['key']))
print(f'载入 {len(items)} 条')

clusters = []
for it in items:
    placed = False
    for c in clusters:
        if is_dup_pair(it['key'], c['keep']['key']):
            c['drops'].append(it); placed = True; break
    if not placed:
        clusters.append({'keep': it, 'drops': []})

multi = [c for c in clusters if c['drops']]
RANK = {'accessible': 0, 'with_code': 1, 'unknown': 2, 'dead': 3}
to_delete = []
for c in multi:
    group = [c['keep']] + c['drops']
    group.sort(key=lambda x: (RANK.get(x['access'], 2), 0 if x['code'] else 1,
                              -(x['size'] or 0), -len(x['key']), x['at'] or ''))
    c['keep'] = group[0]
    to_delete += group[1:]

print(f'重复簇 {len(multi)} 组 | 可删除 {len(to_delete)} 条\n')
print('=== 全部重复组（请人工确认）===')
for c in sorted(multi, key=lambda x: -len(x['drops'])):
    print(f'\n[{len(c["drops"])+1} 个] ✔ 保留: {c["keep"]["title"][:66]}')
    for d in c['drops']:
        print(f'        ✘ 删: {d["title"][:66]}')

if APPLY:
    for d in to_delete:
        cur.execute('DELETE FROM saved WHERE url=?', (d['url'],))
    con.commit()
    print(f'\n已删除 {len(to_delete)} 条, 剩余 {cur.execute("SELECT COUNT(*) FROM saved").fetchone()[0]} 条')
else:
    print('\n[预演] 加 --apply 执行')
con.close()
