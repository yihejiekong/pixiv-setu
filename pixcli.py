# -*- coding: utf-8 -*-
"""随机色图取图 CLI（App API 池版 + 指定画师版）。

打分 = w2·S2 + w1·S1 + w3·S3（权重 2 > 1 > 3）
  S1 偏好命中：item.tags ∩ 163 个 tag，命中数按 1/2/3 封顶（超 3 个不再加分）
  S2 质量分：0.5×全池分位^0.75 + 0.5×绝对质量（bm 权重 0.75 / view 权重 0.25）
  S3 画师分：uid 在 82 位候选画师里记 1
最终在总分前 30% 里随机抽一张。

指定画师（--artist NAME）：模糊匹配画师名，实时抓该画师作品（App API），
不限 tag / AI / R18，只在「质量分前 80%」里均匀随机抽。
"""
import argparse
import json
import math
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
POOL = os.path.join(ROOT, 'pool.json')
POOL_META = os.path.join(ROOT, 'pool_meta.json')
DB2 = os.environ.get('PIX_DB') or os.path.join(ROOT, 'db.json')
HIST = os.path.join(ROOT, 'pick_hist.json')
ENV = os.path.join(ROOT, 'env')
CACHE = os.path.join(ROOT, 'out')

W1, W2, W3 = 0.6, 1.0, 0.3          # 规则权重：2 > 1 > 3
BM_W = float(os.environ.get('PIX_BM_W', '0.75'))     # 收藏分权重（提高）
VIEW_W = float(os.environ.get('PIX_VIEW_W', '0.25'))  # 浏览分权重（降低）
TOP_RATIO = float(os.environ.get('PIX_TOP_RATIO', '0.30'))
ART_TOP_RATIO = float(os.environ.get('PIX_ART_TOP_RATIO', '0.80'))   # 指定画师：保留质量前 80%
ART_PAGES = int(os.environ.get('PIX_ART_PAGES_LIVE', '5'))
AI_RATIO = float(os.environ.get('PIX_AI_RATIO', '0.30'))
AI_S2_MARGIN = float(os.environ.get('PIX_AI_S2_MARGIN', '1.25'))
MIN_BM = int(os.environ.get('PIX_MIN_BM_ABS', '400'))
MIN_VIEW = int(os.environ.get('PIX_MIN_VIEW_ABS', '8000'))
MIN_BM_R18 = int(os.environ.get('PIX_MIN_BM_ABS_R18', '150'))
MIN_VIEW_R18 = int(os.environ.get('PIX_MIN_VIEW_ABS_R18', '2000'))
BM_REF = float(os.environ.get('PIX_BM_REF', '3000'))
VIEW_REF = float(os.environ.get('PIX_VIEW_REF', '30000'))
MAX_SIDE = int(os.environ.get('PIX_MAX_SIDE', '1600'))
MAX_BYTES = int(os.environ.get('PIX_MAX_BYTES', '1500000'))
BUDGET = float(os.environ.get('PIX_PICK_BUDGET', '60'))
ALLOW_R18 = os.environ.get('PIX_ALLOW_R18', '1') == '1'
SOCKS = os.environ.get('PIX_SOCKS', '127.0.0.1:10808')

from PIL import Image, ImageFile                       # noqa: E402
ImageFile.LOAD_TRUNCATED_IMAGES = True

CLIENT_ID = 'MOBrBDS8blbauoSck0ZfDbtuzpyT'
CLIENT_SECRET = 'lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj'
_UA = ('PixivAndroidApp/6.202.1 (Android 12; Pixel 5) '
       'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36')


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 基础工具

def load_env():
    out = {}
    try:
        with open(ENV, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip()
    except Exception:                                      # noqa: BLE001
        pass
    return out


def curl_json(url, headers=None, use_socks=True, timeout=25):
    cmd = ['curl', '-s', '-m', str(timeout)]
    if use_socks:
        cmd += ['--socks5-hostname', SOCKS]
    for k, v in (headers or {}).items():
        cmd += ['-H', '%s: %s' % (k, v)]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True)
    try:
        return json.loads(r.stdout.decode('utf-8', 'replace'))
    except Exception:                                      # noqa: BLE001
        return None


def access_token():
    env = load_env()
    rt = env.get('PIX_REFRESH_TOKEN') or os.environ.get('PIX_REFRESH_TOKEN')
    if not rt:
        return None
    cmd = ['curl', '-s', '-m', '25', '--socks5-hostname', SOCKS, '-X', 'POST',
           'https://oauth.secure.pixiv.net/auth/token',
           '-H', 'User-Agent: ' + _UA,
           '-H', 'Content-Type: application/x-www-form-urlencoded',
           '--data-urlencode', 'client_id=' + CLIENT_ID,
           '--data-urlencode', 'client_secret=' + CLIENT_SECRET,
           '--data-urlencode', 'grant_type=refresh_token',
           '--data-urlencode', 'refresh_token=' + rt,
           '--data-urlencode', 'include_policy=true',
           '--data-urlencode', 'get_secure_url=true']
    r = subprocess.run(cmd, capture_output=True)
    try:
        j = json.loads(r.stdout.decode('utf-8', 'replace'))
        return (j.get('response') or {}).get('access_token')
    except Exception:                                      # noqa: BLE001
        return None


def load_pool():
    with open(POOL, encoding='utf-8') as f:
        raw = json.load(f)
    items = raw.values() if isinstance(raw, dict) else raw
    return {str(x.get('pid') or x.get('p')): _norm(x) for x in items}


def _norm(it):
    """兼容旧池（p/u/a/t/r/s/src/tg）与新池（pid/uid/author/tags/x/...）两种字段。"""
    return {
        'pid': str(it.get('pid') or it.get('p') or ''),
        'uid': str(it.get('uid') or it.get('u') or ''),
        'author': it.get('author') or it.get('a') or '',
        'title': it.get('title') or '',
        'tags': list(it.get('tags') or it.get('t') or []),
        'x': bool(it.get('x')) or bool(int(it.get('r') or 0)),
        'ai': bool(it.get('ai')),
        'bm': int(it.get('bm') or 0),
        'view': int(it.get('view') or 0),
        'pg': int(it.get('pg') or 1),
        'w': int(it.get('w') or 0),
        'h': int(it.get('h') or 0),
        'artist': bool(it.get('artist')) or it.get('src') == 'artist',
        'hidden': bool(it.get('hidden')),
    }


def load_db():
    try:
        with open(DB2, encoding='utf-8') as f:
            return json.load(f)
    except Exception:                                      # noqa: BLE001
        return {'tags': [], 'artists': [], 'fuzzy': [], 'alias': []}


def tag_set(db):
    s = set()
    for t in db.get('tags') or []:
        jp = t.get('jp') if isinstance(t, dict) else str(t)
        if jp:
            s.add(jp.strip())
    return s


def artist_set(db):
    s = set()
    for a in db.get('artists') or []:
        s.add(str(a.get('uid')))
    return s


# ---------------------------------------------------------------- 词 / 画师解析

def resolve_word(db, word):
    """模糊词（中文 / 别名 / 日文 tag）-> 命中的 tag 列表。

    兼容 db2.json 的两套结构：
      alias: {"肚脐": ["おへそ", ...]}  或  [{"word": ..., "tags": [...]}, ...]
      fuzzy: [{"word": "肚脐", "hits": [{"jp": "おへそ", "total": N}, ...]}, ...]
    """
    if not word:
        return None
    w = str(word).strip()
    tags = [t for t in (db.get('tags') or []) if isinstance(t, dict) and t.get('jp')]
    jps = [t['jp'] for t in tags]

    # 1) 日文 tag 精确
    if w in jps:
        return [w]

    # 2) alias 索引（dict 或 list 两种写法）
    pairs = []
    als = db.get('alias') or []
    if isinstance(als, dict):
        for k, v in als.items():
            vals = [str(x) for x in (v or [])] if isinstance(v, (list, tuple)) else [str(v)]
            if k and vals:
                pairs.append((str(k), vals))
    else:
        for a in als:
            if not isinstance(a, dict):
                continue
            k = str(a.get('word') or a.get('alias') or a.get('cn') or a.get('name') or '')
            v = a.get('tags') or a.get('jp') or []
            if isinstance(v, str):
                v = [v]
            v = [str(x) for x in (v or [])]
            if k and v:
                pairs.append((k, v))
    exact, part = [], []
    for k, v in pairs:
        if k == w:
            exact.extend(v)
        elif w in k or k in w:
            part.extend(v)
    if exact:
        return sorted(set(exact))[:8]

    # 3) fuzzy 索引（hits / tags / main 三种字段）
    for f in db.get('fuzzy') or []:
        if not isinstance(f, dict):
            continue
        key = str(f.get('word') or f.get('kw') or f.get('phrase') or '')
        if key != w:
            continue
        out = []
        for h in f.get('hits') or []:
            if isinstance(h, dict):
                jp = h.get('jp') or h.get('tag')
                if jp:
                    out.append(str(jp))
            elif h:
                out.append(str(h))
        if not out:
            v = f.get('tags') or f.get('main') or []
            if isinstance(v, str):
                v = [v]
            out = [str(x) for x in (v or [])]
        if out:
            return out[:8]

    # 4) 中文名 / 别名精确（tags 表里的 cn 与 aliases）
    for t in tags:
        cn = str(t.get('cn') or '')
        aliases = [str(x) for x in (t.get('aliases') or [])]
        if w == cn or w in aliases:
            return [t['jp']]

    if part:
        return sorted(set(part))[:8]

    # 5) 日文 tag 包含
    low = w.lower()
    hit = [t for t in jps if low in t.lower()]
    if hit:
        return hit[:6]
    return None


def resolve_artist(db, name):
    """模糊匹配画师：返回 [(uid, name, star)]，精确命中优先。"""
    if not name:
        return []
    w = str(name).strip()
    arts = [a for a in (db.get('artists') or []) if isinstance(a, dict)]
    exact = [a for a in arts if str(a.get('name') or '').strip() == w]
    if exact:
        return [(str(a.get('uid')), a.get('name'), a.get('star')) for a in exact[:3]]
    part = [a for a in arts if w in str(a.get('name') or '')]
    if part:
        part.sort(key=lambda a: -float(a.get('star') or 0))
        return [(str(a.get('uid')), a.get('name'), a.get('star')) for a in part[:8]]
    al = [a for a in (db.get('alias') or [])
          if isinstance(a, dict) and w in str(a.get('word') or a.get('name') or '')
          and str(a.get('uid') or '').isdigit()]
    if al:
        return [(str(a.get('uid')), a.get('word') or a.get('name'), 3) for a in al[:5]]
    return []


# ---------------------------------------------------------------- 打分

def quality(it):
    return BM_W * math.log10(int(it.get('bm') or 0) + 1) + \
        VIEW_W * math.log10(int(it.get('view') or 0) + 1)


def abs_quality(it):
    """绝对质量，不依赖池子分布：bm/view 各自对参考值取对数后归一。"""
    b = math.log10(int(it.get('bm') or 0) + 1) / math.log10(BM_REF + 1)
    v = math.log10(int(it.get('view') or 0) + 1) / math.log10(VIEW_REF + 1)
    return min(1.0, BM_W * b + VIEW_W * v)


def percentile(values, v):
    if not values:
        return 0.0
    lo, hi = 0, len(values)
    while lo < hi:
        mid = (lo + hi) // 2
        if values[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return lo / max(1, len(values) - 1)


def score_candidates(cand, hits, ref=None):
    """给候选集打分，返回 [(score, s1, s2, s3, item)] 降序。"""
    ref = ref if ref else cand
    qs = sorted(quality(x) for x in ref)
    scored = []
    for it in cand:
        n_hit = len(set(it.get('tags') or []) & hits)
        s1 = min(3, n_hit) / 3.0
        s2 = 0.5 * (percentile(qs, quality(it)) ** 0.75) + 0.5 * abs_quality(it)
        s3 = 1.0 if it.get('artist') else 0.0
        score = W2 * s2 + W1 * s1 + W3 * s3
        scored.append((score, s1, s2, s3, it))
    scored.sort(key=lambda x: -x[0])
    return scored


def hist_load():
    try:
        with open(HIST, encoding='utf-8') as f:
            h = json.load(f)
    except Exception:                                      # noqa: BLE001
        return {'runs': []}
    if isinstance(h, list):
        runs = [r if isinstance(r, dict) else {'ai': bool(r)} for r in h]
        return {'runs': runs}
    if not isinstance(h, dict):
        return {'runs': []}
    h.setdefault('runs', [])
    return h


def hist_save(h):
    tmp = HIST + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(h, f, ensure_ascii=False)
    os.replace(tmp, HIST)


def ai_gate(h):
    runs = h.get('runs') or []
    n = len(runs)
    ai = sum(1 for r in runs if r.get('ai'))
    credit = AI_RATIO * n - ai
    recent = runs[-10:]
    w10 = sum(1 for r in recent if r.get('ai'))
    return credit >= 1.0 and w10 < max(1, int(AI_RATIO * 10))


# ---------------------------------------------------------------- 取图

def shrink(path):
    im = Image.open(path)
    if im.width > MAX_SIDE or im.height > MAX_SIDE:
        r = MAX_SIDE / float(max(im.width, im.height))
        im = im.resize((int(im.width * r), int(im.height * r)), Image.LANCZOS)
    im = im.convert('RGB')
    for q in (92, 88, 84, 80, 76):
        im.save(path, 'JPEG', quality=q, optimize=True, progressive=True)
        if os.path.getsize(path) <= MAX_BYTES:
            break
    return os.path.getsize(path)


def download(it, out, budget_end):
    pid, pg = it['pid'], int(it.get('pg') or 1)
    for page in range(min(pg, 2)):
        if time.time() > budget_end:
            break
        url = 'https://pixiv.re/%s%s.jpg' % (pid, '' if page == 0 else '-%d' % page)
        r = subprocess.run(['curl', '-sL', '-m', '15', '-o', out, url], capture_output=True)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 20000:
            try:
                shrink(out)
                return page, os.path.getsize(out)
            except Exception as e:                          # noqa: BLE001
                log('   图片处理失败 %s: %s' % (pid, e))
    return None, 0


def _result(ok, it, got, t0, extra=None):
    res = {
        'ok': True, 'pid': it['pid'], 'author': it['author'], 'title': it['title'],
        'tags': (it.get('tags') or [])[:8], 'r18': bool(it.get('x')),
        'ai': bool(it.get('ai')), 'bm': it.get('bm'), 'view': it.get('view'),
        'page': got[0], 'size': got[1],
        'secs': round(time.time() - t0, 1),
    }
    if extra:
        res.update(extra)
    return res


def _try_download(order, out, t0):
    budget_end = t0 + BUDGET
    tries, got, hit = 0, None, None
    for alt in order:
        tries += 1
        page, size = download(alt[4], out, budget_end)
        if size:
            got = (page, size)
            hit = alt
            break
    return hit, got, tries


# ---------------------------------------------------------------- 抽图：池内

def pick(mode, fuzzy, exclude, out):
    t0 = time.time()
    pool = load_pool()
    db = load_db()
    hits = tag_set(db)

    want_r18 = mode == 'lewd'
    cand = [x for x in pool.values() if bool(x.get('x')) == want_r18]
    if want_r18:
        min_bm, min_view = MIN_BM_R18, MIN_VIEW_R18
    else:
        min_bm, min_view = MIN_BM, MIN_VIEW
    strong = [x for x in cand if x['bm'] >= min_bm or x['view'] >= min_view]
    if strong:
        cand = strong
    else:
        log('!! 绝对门槛过滤后为空（min_bm=%d/min_view=%d），本次退回全量候选' % (min_bm, min_view))
    if want_r18 and not ALLOW_R18:
        return {'ok': False, 'reason': 'r18_disabled'}

    tag_words = None
    if fuzzy:
        tag_words = resolve_word(db, fuzzy)
        if not tag_words:
            return {'ok': False, 'reason': 'unknown_word', 'word': fuzzy}
        cand = [x for x in cand if set(x.get('tags') or []) & set(tag_words)]
    if not cand:
        return {'ok': False, 'reason': 'empty', 'mode': mode, 'fuzzy': fuzzy}

    if exclude:
        ex = set(str(p) for p in exclude)
        left = [x for x in cand if x['pid'] not in ex]
        if left:
            cand = left
        else:
            log('!! 7 天内已发的图覆盖了整个候选集，本次放宽限制')

    scored = score_candidates(cand, hits, ref=list(pool.values()))
    top_n = max(1, int(len(scored) * TOP_RATIO))
    top = scored[:top_n]

    human = [x for x in scored if not x[4].get('ai')]
    ai_pool = []
    if human and ai_gate(hist_load()):
        med = human[len(human) // 2][0]
        need = max(med * AI_S2_MARGIN, 0.45)
        ai_pool = [x for x in top if x[4].get('ai') and x[2] >= need]
        if not ai_pool:
            ai_pool = [x for x in scored if x[4].get('ai') and x[2] >= need][:max(1, top_n // 3)]

    bag = [x for x in top if not x[4].get('ai')] or top
    chosen_pool = ai_pool if ai_pool else bag
    pick_item = random.choice(chosen_pool)

    order = [pick_item] + [x for x in scored if x is not pick_item][:6]
    hit, got, tries = _try_download(order, out, t0)
    if not got:
        return {'ok': False, 'reason': 'download_failed', 'pid': pick_item[4]['pid']}

    score, s1, s2, s3, it = hit
    h = hist_load()
    h.setdefault('runs', []).append({'ts': int(time.time()), 'pid': it['pid'],
                                     'ai': bool(it.get('ai')),
                                     'mode': mode, 'fuzzy': fuzzy or ''})
    h['runs'] = h['runs'][-400:]
    hist_save(h)

    extra = {'file': out, 'score': round(score, 4), 's1': round(s1, 3),
             's2': round(s2, 3), 's3': round(s3, 3),
             'hit_tags': sorted(set(it.get('tags') or []) & hits)[:5],
             'cand': len(cand), 'top': top_n, 'ai_used': bool(ai_pool), 'tries': tries}
    if tag_words:
        extra['word_tags'] = tag_words[:6]
    return _result(True, it, got, t0, extra)


# ---------------------------------------------------------------- 抽图：指定画师

def _artist_items(uid, token):
    """拉该画师的作品（App API，最多 ART_PAGES 页）。"""
    items, next_url = [], None
    for page in range(ART_PAGES):
        url = next_url or ('https://app-api.pixiv.net/v1/user/illusts?user_id=%s'
                           '&type=illust&filter=for_android' % uid)
        j = curl_json(url, headers={'Authorization': 'Bearer ' + token,
                                    'User-Agent': _UA})
        if not j or j.get('error'):
            log('   画师 %s 第 %d 页失败：%s' % (uid, page + 1, (j or {}).get('error')))
            break
        rows = j.get('illusts') or []
        for r in rows:
            if int(r.get('width') or 0) < 600 and int(r.get('height') or 0) < 600:
                continue
            items.append({
                'pid': str(r.get('id')), 'uid': str(uid),
                'author': (r.get('user') or {}).get('name') or '',
                'title': r.get('title') or '',
                'tags': [t.get('name') for t in (r.get('tags') or []) if isinstance(t, dict)],
                'x': int(r.get('x_restrict') or 0) > 0,
                'ai': int(r.get('illust_ai_type') or 0) == 2,
                'bm': int(r.get('total_bookmarks') or 0),
                'view': int(r.get('total_view') or 0),
                'pg': int(r.get('page_count') or 1),
                'w': int(r.get('width') or 0), 'h': int(r.get('height') or 0),
                'artist': True,
            })
        next_url = j.get('next_url')
        if not next_url:
            break
    seen, uniq = set(), []
    for it in items:
        if it['pid'] in seen:
            continue
        seen.add(it['pid'])
        uniq.append(it)
    return uniq


def pick_artist(name, exclude, out):
    t0 = time.time()
    db = load_db()
    cands = resolve_artist(db, name)
    if not cands:
        names = [str(a.get('name')) for a in (db.get('artists') or []) if isinstance(a, dict)]
        return {'ok': False, 'reason': 'unknown_artist', 'name': name,
                'suggest': names[:8]}
    token = access_token()
    if not token:
        return {'ok': False, 'reason': 'no_token'}

    uid, aname, star = cands[0]
    works = _artist_items(uid, token)
    if not works:
        return {'ok': False, 'reason': 'artist_empty', 'name': aname, 'uid': uid}

    if exclude:
        ex = set(str(p) for p in exclude)
        left = [w for w in works if w['pid'] not in ex]
        if left:
            works = left

    works.sort(key=lambda w: -quality(w))
    keep = max(1, int(len(works) * ART_TOP_RATIO))
    pool = works[:keep]
    pick_item = random.choice(pool)
    order = [(0.0, 0.0, 0.0, 0.0, pick_item)]
    order += [(0.0, 0.0, 0.0, 0.0, x) for x in pool if x is not pick_item][:6]
    hit, got, tries = _try_download(order, out, t0)
    if not got:
        return {'ok': False, 'reason': 'download_failed', 'pid': pick_item['pid']}

    it = hit[4]
    extra = {'file': out, 'artist_name': aname, 'artist_uid': uid,
             'artist_star': star, 'works': len(works), 'kept': keep,
             'rank': pool.index(it) + 1, 'tries': tries,
             'hit_tags': [t for t in (it.get('tags') or [])
                          if t in tag_set(db)][:5]}
    return _result(True, it, got, t0, extra)


# ---------------------------------------------------------------- 列表

def list_artists(keyword=None):
    db = load_db()
    arts = [a for a in (db.get('artists') or []) if isinstance(a, dict)]
    if keyword:
        kw = str(keyword).strip()
        arts = [a for a in arts if kw in str(a.get('name') or '')]
    arts.sort(key=lambda a: (-float(a.get('star') or 0), str(a.get('name'))))
    rows = [{'name': a.get('name'), 'uid': str(a.get('uid')),
             'star': a.get('star')} for a in arts]
    return {'ok': True, 'total': len(rows), 'artists': rows}


def list_tags(keyword=None):
    db = load_db()
    rows = []
    for t in (db.get('tags') or []):
        if not isinstance(t, dict):
            continue
        jp = str(t.get('jp') or '')
        if not jp:
            continue
        if keyword and str(keyword) not in jp:
            continue
        rows.append({'jp': jp, 'cn': t.get('cn') or '', 'pri': t.get('pri')})
    rows.sort(key=lambda r: (-int(r.get('pri') or 0), r['jp']))
    return {'ok': True, 'total': len(rows), 'tags': rows}


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['pick', 'status', 'tags', 'artists', 'build'])
    ap.add_argument('--mode', default='anime', choices=['anime', 'lewd'])
    ap.add_argument('--fuzzy')
    ap.add_argument('--artist')
    ap.add_argument('--exclude')
    ap.add_argument('--out', default=os.path.join(CACHE, 'pick.jpg'))
    ap.add_argument('--word')
    ap.add_argument('--keyword')
    a = ap.parse_args()

    if a.cmd == 'status':
        try:
            meta = json.load(open(POOL_META, encoding='utf-8'))
        except Exception:                                  # noqa: BLE001
            meta = {}
        print(json.dumps(meta, ensure_ascii=False, indent=1))
        return
    if a.cmd == 'tags':
        if a.word:
            db = load_db()
            print(json.dumps(resolve_word(db, a.word), ensure_ascii=False))
        else:
            print(json.dumps(list_tags(a.keyword), ensure_ascii=False))
        return
    if a.cmd == 'artists':
        print(json.dumps(list_artists(a.keyword), ensure_ascii=False))
        return
    if a.cmd == 'build':
        os.execv(sys.executable, [sys.executable, os.path.join(ROOT, 'build_pool3.py')])
        return

    os.makedirs(CACHE, exist_ok=True)
    excl = [x for x in (a.exclude or '').split(',') if x.strip()]
    if a.artist:
        res = pick_artist(a.artist, excl, a.out)
    else:
        res = pick(a.mode, a.fuzzy, excl, a.out)
    print(json.dumps(res, ensure_ascii=False))


if __name__ == '__main__':
    main()
