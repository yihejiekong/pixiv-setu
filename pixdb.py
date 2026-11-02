# -*- coding: utf-8 -*-
"""第 2 批数据库解析：画师 / Tag 库 / 模糊词索引 / 别称 -> db2.json

不依赖列位置，按表头名取值；缺列会写进 problems，方便换表时排查。
"""
from __future__ import annotations

import hashlib
import json
import os
import re

import openpyxl

SHEET_ARTIST = ('画师',)
SHEET_TAG = ('日文Tag', 'Tag库')
SHEET_FUZZY = ('模糊词',)
SHEET_ALIAS = ('别称',)

TAGVOL_RE = re.compile(r'([^\s/()（）,，]+)\s*[（(]\s*([\d,]+)\s*[)）]')
SPLIT_RE = re.compile(r'[；;、,，/｜|]+')


def _norm(s):
    return re.sub(r'\s+', '', str(s or '')).lower()


def _pick_sheet(wb, keys):
    for name in wb.sheetnames:
        n = _norm(name)
        if all(k.lower() in n for k in keys):
            return wb[name]
    return None


def _header(ws, max_scan=14):
    """在前若干行里找表头行，返回 (row_index, {表头: 列号})。"""
    best = (None, {})
    for r in range(1, min(ws.max_row, max_scan) + 1):
        cols = {}
        for c in range(1, ws.max_column + 1):
            v = ws.cell(r, c).value
            if v is None:
                continue
            k = _norm(v)
            if k and k not in cols:
                cols[k] = c
        # 列数更多才算更可能是表头；同列数保留靠前那一行（数据行常常一样宽）
        if len(cols) > len(best[1]) and len(cols) >= 2:
            best = (r, cols)
        if len(best[1]) >= 6:
            break
    return best


def _get(ws, row, cols, *names, default=None):
    for n in names:
        c = cols.get(_norm(n))
        if c:
            v = ws.cell(row, c).value
            if v is not None and str(v).strip() != '':
                return v
    return default


def _star(v, default=3.0):
    try:
        f = float(str(v).replace('★', '').strip())
        return max(0.0, min(5.0, f))
    except Exception:
        return default


def _num(v, default=0):
    try:
        return int(re.sub(r'[^\d]', '', str(v)) or 0)
    except Exception:
        return default


def parse_tags_cell(text):
    """'黒タイツ(63263) / 黒スト(46811)' -> [{'jp','total'}]"""
    out = []
    for m in TAGVOL_RE.finditer(str(text or '')):
        jp = m.group(1).strip()
        if jp and jp not in [x['jp'] for x in out]:
            out.append({'jp': jp, 'total': int(m.group(2).replace(',', ''))})
    return out


def default_xlsx():
    """db/ 下第一个 .xlsx（多份会自动合并，可放多批）。"""
    import glob
    root = os.path.dirname(os.path.abspath(__file__))
    hits = sorted(glob.glob(os.path.join(root, 'db', '*.xlsx')))
    if not hits:
        raise SystemExit('db/ 里没有 .xlsx：把偏好表放进去（可多份）')
    return hits[0]


def load(path, extra_paths=()):
    paths = [path] + [p for p in extra_paths if p]
    problems = []
    artists, tags, fuzzy = [], [], []
    alias = {}
    seen_uid, seen_tags, seen_words = set(), set(), set()
    for p in paths:
        if not os.path.exists(p):
            problems.append('文件不存在：%s' % p)
            continue
        wb = openpyxl.load_workbook(p, data_only=True)

        # 画师
        ws = _pick_sheet(wb, SHEET_ARTIST)
        if ws is None:
            problems.append('找不到画师 sheet')
        else:
            hr, cols = _header(ws)
            hr = hr or 0
            if '推荐星级' not in cols:
                problems.append('画师表没有「推荐星级」列，全部按 3 星处理')
            if '用户id' not in cols:
                problems.append('画师表没有「用户ID」列，无法取样')
            for r in range(hr + 1, ws.max_row + 1):
                uid = _get(ws, r, cols, '用户ID')
                if uid is None:
                    continue
                uid = str(uid).strip()
                if not uid.isdigit() or uid in seen_uid:
                    continue
                seen_uid.add(uid)
                artists.append({
                    'uid': uid,
                    'name': str(_get(ws, r, cols, '画师名称', default='')).strip(),
                    'star': _star(_get(ws, r, cols, '推荐星级')),
                    'tags': str(_get(ws, r, cols, '擅长题材', default='')).strip(),
                })

        # Tag 库
        ws = _pick_sheet(wb, SHEET_TAG)
        if ws is None:
            problems.append('找不到日文 Tag 库 sheet')
        else:
            hr, cols = _header(ws)
            hr = hr or 0
            if '日文tag' not in cols:
                problems.append('Tag 库没有「日文Tag」列')
            for r in range(hr + 1, ws.max_row + 1):
                jp = _get(ws, r, cols, '日文Tag')
                if not jp:
                    continue
                jp = str(jp).strip()
                if jp in seen_tags:
                    continue
                seen_tags.add(jp)
                al = str(_get(ws, r, cols, '中文别称（口语触发词）', '中文别称', default='') or '')
                tags.append({
                    'jp': jp,
                    'cn': str(_get(ws, r, cols, '中文解释', '中文标准名', default='') or '').strip(),
                    'aliases': [x for x in SPLIT_RE.split(al) if x.strip()],
                    'cat': str(_get(ws, r, cols, '分类', default='') or '').strip(),
                    'total': _num(_get(ws, r, cols, '投稿总量(实测)', '实测投稿量', '投稿总量')),
                    'star': _star(_get(ws, r, cols, '推荐星级')),
                })
                for a in tags[-1]['aliases']:
                    alias.setdefault(a.strip(), [])
                    if jp not in alias[a.strip()]:
                        alias[a.strip()].append(jp)

        # 模糊词
        ws = _pick_sheet(wb, SHEET_FUZZY)
        if ws is None:
            problems.append('找不到模糊词索引 sheet')
        else:
            hr, cols = _header(ws)
            hr = hr or 0
            if not any('模糊词' in k for k in cols):
                problems.append('模糊词表没有「模糊词」列')
            for r in range(hr + 1, ws.max_row + 1):
                w = _get(ws, r, cols, '模糊词（用户会说的）', '模糊词')
                if not w:
                    continue
                w = str(w).strip()
                if w in seen_words:
                    continue
                seen_words.add(w)
                hits = parse_tags_cell(_get(ws, r, cols, '全部命中Tag（语义顺序，括号为实测投稿量）', '全部命中Tag'))
                if not hits:
                    hits = parse_tags_cell(_get(ws, r, cols, '主Tag'))
                fuzzy.append({
                    'word': w,
                    'type': str(_get(ws, r, cols, '词类型', default='') or '').strip(),
                    'pri': _num(_get(ws, r, cols, '优先级(5=核心)', '优先级'), default=3) or 3,
                    'main': str(_get(ws, r, cols, '主Tag', default='') or '').strip(),
                    'hits': hits,
                    'behavior': str(_get(ws, r, cols, '机器人默认行为', default='') or '').strip(),
                })

        # 别称明细（补充别名映射）
        ws = _pick_sheet(wb, SHEET_ALIAS)
        if ws is not None:
            hr, cols = _header(ws)
            hr = hr or 0
            for r in range(hr + 1, ws.max_row + 1):
                jp = _get(ws, r, cols, '日文Tag')
                al = _get(ws, r, cols, '中文别称（全部，；分隔）', '中文别称')
                if not jp or not al:
                    continue
                for a in SPLIT_RE.split(str(al)):
                    a = a.strip()
                    if a:
                        alias.setdefault(a, [])
                        if str(jp).strip() not in alias[a]:
                            alias[a].append(str(jp).strip())

    fp = hashlib.sha1()
    for p in paths:
        if os.path.exists(p):
            fp.update(open(p, 'rb').read())
    db = {
        'artists': sorted(artists, key=lambda x: (-x['star'], x['uid'])),
        'tags': tags,
        'fuzzy': fuzzy,
        'alias': alias,
        'meta': {
            'files': [os.path.basename(p) for p in paths if os.path.exists(p)],
            'fingerprint': fp.hexdigest(),
            'artists': len(artists),
            'tags': len(tags),
            'fuzzy': len(fuzzy),
            'aliases': len(alias),
        },
        'problems': problems,
    }
    return db


if __name__ == '__main__':
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    p = sys.argv[1] if len(sys.argv) > 1 else default_xlsx()
    d = load(p)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'db.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False)
    print('写到', out)
    print(json.dumps(d['meta'], ensure_ascii=False, indent=1))
    print('problems:', d['problems'])
