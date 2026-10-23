# -*- coding: utf-8 -*-
"""verify_access.py — 统一的「可获取性」验证器（存活 + 码校验一体）

状态机（按用户需求定义）:
    accessible   ✅ 可直接获取   链接存活 + 无需码
    with_code    🔑 凭码可获取   链接存活 + 有码且码正确
    bad_code     ⛔ 码错误       链接存活 + 需码 + 有码但错误
    no_code      🔒 缺码         链接存活 + 需码 + 没码
    dead         ❌ 已失效       链接不存在/被取消/过期
    unknown      ⚪ 未验证       网络异常等

技术上利用两个网盘的错误码精确区分（已实测）:
    夸克: 41008=需要码  41007=码错误  有stoken=通过
    百度: -12=需要码    -9=码错误    0=通过
"""
import time, json, re, os, sys
import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

UA_PC = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

LABELS = {
    'accessible': '✅ 可直接获取',
    'with_code': '🔑 凭码可获取',
    'bad_code': '⛔ 码错误',
    'no_code': '🔒 缺码',
    'dead': '❌ 已失效',
    'unknown': '⚪ 未验证',
}
# 能真正拿到资源的两种状态
USABLE = ('accessible', 'with_code')


def verify_quark(pid, passcode='', with_code_check=True):
    """返回 (state, detail)"""
    try:
        with httpx.Client(timeout=15, headers={'User-Agent': UA_PC, 'Referer': 'https://pan.quark.cn/'}) as c:
            def token(pwd):
                return c.post('https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc',
                              json={'pwd_id': pid, 'passcode': pwd}).json()

            j = token('')
            st = (j.get('data') or {}).get('stoken')
            if st:
                return 'accessible', {'stoken': True}
            code_num = j.get('code') or j.get('error_code')
            msg = str(j.get('message', ''))
            # 需码
            if code_num in (41008,) or '需要提取码' in msg:
                if not passcode:
                    return 'no_code', {'code_num': code_num}
                if not with_code_check:
                    return 'with_code', {'code_num': code_num, 'unchecked': True}
                j2 = token(passcode)
                if (j2.get('data') or {}).get('stoken'):
                    return 'with_code', {'verified': True}
                c2 = j2.get('code') or j2.get('error_code')
                if c2 in (41007,) or '错误' in str(j2.get('message', '')):
                    return 'bad_code', {'code_num': c2}
                return 'bad_code', {'code_num': c2, 'msg': str(j2.get('message', ''))[:30]}
            # 其他错误码判断是否失效
            if code_num in (41004, 41005, 41006) or '取消' in msg or '失效' in msg or '不存在' in msg:
                return 'dead', {'code_num': code_num, 'msg': msg[:30]}
            # 有码但没传，先试空码已失败
            if passcode:
                j2 = token(passcode)
                if (j2.get('data') or {}).get('stoken'):
                    return 'with_code', {'verified': True}
                return 'bad_code', {'code_num': j2.get('code')}
            return 'unknown', {'code_num': code_num, 'msg': msg[:40]}
    except Exception as e:
        return 'unknown', {'err': str(e)[:50]}


def verify_baidu(short, code='', with_code_check=True):
    try:
        with httpx.Client(follow_redirects=True, timeout=18) as c:
            url = f'https://pan.baidu.com/s/{short}'
            r0 = c.get(url, headers={'User-Agent': UA_PC, 'Referer': 'https://pan.baidu.com/disk/home'})
            page = r0.text
            # 直接可见文件列表 = 无需码
            if '"file_list"' in page or 'server_filename' in page:
                if '请输入提取码' not in page:
                    return 'accessible', {}
            # 失效判断
            for kw in ('分享的文件已经被取消', '分享已过期', '你访问的页面不存在', '你所访问的页面不存在', '链接已失效'):
                if kw in page:
                    return 'dead', {'kw': kw}
            need = ('请输入提取码' in page) or ('提取文件' in page) or ('"file_list"' not in page)
            if not need:
                return 'accessible', {}
            surl = short[1:] if short.startswith('1') else short

            def verify(pwd):
                r = c.post('https://pan.baidu.com/share/verify',
                           params={'surl': surl, 't': str(int(time.time() * 1000)), 'channel': 'chunlei',
                                   'web': '1', 'app_id': '250528', 'clienttype': '0'},
                           data={'pwd': pwd, 'vcode': '', 'vcode_str': ''},
                           headers={'User-Agent': UA_PC, 'Referer': url})
                try:
                    return r.json().get('errno')
                except Exception:
                    return None

            e0 = verify('')
            if e0 == 0:
                return 'accessible', {'verify_empty_ok': True}
            if e0 == -12 or '请输入提取码' in page:
                if not code:
                    return 'no_code', {'errno': e0}
                if not with_code_check:
                    return 'with_code', {'unchecked': True}
                e1 = verify(code)
                if e1 == 0:
                    return 'with_code', {'verified': True}
                if e1 == -9:
                    return 'bad_code', {'errno': e1}
                if e1 == -12:
                    return 'no_code', {'errno': e1}
                return 'unknown', {'errno': e1}
            if e0 == -9:
                # 空码即错码 → 需要码
                if not code:
                    return 'no_code', {'errno': e0}
                if not with_code_check:
                    return 'with_code', {'unchecked': True}
                e1 = verify(code)
                if e1 == 0:
                    return 'with_code', {'verified': True}
                return 'bad_code', {'errno': e1}
            return 'unknown', {'errno': e0}
    except Exception as e:
        return 'unknown', {'err': str(e)[:50]}


def verify_url(url, code='', with_code_check=True):
    """统一入口，返回 (state, detail)"""
    if 'pan.quark.cn/s/' in url:
        pid = url.rstrip('/').rsplit('/', 1)[1].split('?')[0]
        return verify_quark(pid, code or '', with_code_check)
    if 'pan.baidu.com/s/' in url:
        short = url.split('/s/')[1].split('?')[0]
        return verify_baidu(short, code or '', with_code_check)
    if 'alipan.com' in url or 'aliyundrive.com' in url:
        return verify_aliyun(url, code or '')
    # 其他网盘回落到通用验证器
    return 'unknown', {'err': 'unsupported-disk'}


def verify_aliyun(url, code=''):
    try:
        sid = url.rstrip('/').rsplit('/', 1)[1].split('?')[0]
        j = httpx.post('https://api.alipan.com/adrive/v3/share_link/get_share_by_anonymous',
                       headers={'Content-Type': 'application/json'},
                       data=json.dumps({'share_id': sid}), timeout=15).json()
        if j.get('has_pwd') is True:
            return ('with_code' if code else 'no_code'), {'has_pwd': True, 'unchecked': True}
        if j.get('share_name') or j.get('file_count') is not None or 'has_pwd' in j:
            return 'accessible', {}
        code_s = str(j.get('code', ''))
        if 'Expired' in code_s or 'NotFound' in code_s or 'Cancelled' in code_s:
            return 'dead', {'code': code_s}
        return 'unknown', {'code': code_s[:30]}
    except Exception as e:
        return 'unknown', {'err': str(e)[:50]}


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('url')
    ap.add_argument('code', nargs='?', default='')
    a = ap.parse_args()
    st, detail = verify_url(a.url, a.code)
    print(f'{LABELS.get(st, st)}  |  {json.dumps(detail, ensure_ascii=False)}')
