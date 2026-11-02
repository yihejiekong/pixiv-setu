# -*- coding: utf-8 -*-
"""建池：用 pixiv App API 拉「偏好 tag + 候选画师」的作品，产物 pool.json。

每条记录自带 total_bookmarks / total_view / x_restrict / aiType，
省掉逐张详情（那是 429 的根源）。

设计要点
- 不再对画师做强制限制：词池覆盖 163 个 tag 的全域结果，画师池只用于「加分标记」。
- 每个 tag 拉 safe 与 r18（`<tag> R-18` 组合词）两种查询，各取 PAGES 页。
- 单条记录字段：pid/uid/author/title/tags/bm/view/x(是否R18)/ai/pg/w/h/src/tag_hits。
"""
import json
import os
import subprocess
import sys
import time

ROOT = os.environ.get('PIX_ROOT') or os.path.dirname(os.path.abspath(__file__))
DB2 = os.path.join(ROOT, 'db2.json')
OUT = os.path.join(ROOT, 'pool.json')
META = os.path.join(ROOT, 'pool_meta.json')
PAGES = int(os.environ.get('PIX_TAG_PAGES', '1'))
DUMP_EVERY = int(os.environ.get('PIX_DUMP_EVERY', '20'))
ART_PAGES = int(os.environ.get('PIX_ART_PAGES', '2'))
MIN_SIDE = int(os.environ.get('PIX_MIN_SIDE', '600'))
SOCKS = os.environ.get('PIX_SOCKS', '127.0.0.1:10808')
SLEEP = float(os.environ.get('PIX_API_SLEEP', '0.9'))
RATE_SLEEP = float(os.environ.get('PIX_RATE_SLEEP', '45'))
RATE_TRIES = int(os.environ.get('PIX_RATE_TRIES', '4'))
CLIENT_ID = 'MOBrBDS8blbauoSck0ZfDbtuzpyT'
CLIENT_SECRET = 'lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj'
UA = 'PixivAndroidApp/6.202.1 (Android 12)'


def log(*a):
    print(*a, flush=True)


def _curl(url, data=None, token=None):
    args = ['curl', '-s', '-m', '40', '--socks5-hostname', SOCKS,
            '-H', 'User-Agent: ' + UA, '-H', 'App-OS: android',
            '-H', 'App-OS-Version: 12', '-H', 'App-Version: 6.202.1']
    if token:
        args += ['-H', 'Authorization: Bearer ' + token]
    if data:
        args += ['-X', 'POST', '-H', 'Content-Type: application/x-www-form-urlencoded',
                 '--data', data]
    args.append(url)
    out = subprocess.run(args, capture_output=True).stdout.decode('utf-8', 'replace')
    try:
        return json.loads(out)
    except Exception:                                          # noqa: BLE001
        return {'_raw': out[:200]}


def curl(url, data=None, token=None):
    d = {}
    for attempt in range(RATE_TRIES):
        d = _curl(url, data=data, token=token)
        blob = (d.get('_raw') or '') + json.dumps(d.get('error') or {}, ensure_ascii=False)
        if 'Rate Limit' in blob or '429' in blob:
            log('   ~~ rate limit，等 %ds 后重试（%d/%d）' % (RATE_SLEEP, attempt + 1, RATE_TRIES))
            time.sleep(RATE_SLEEP)
            continue
        return d
    return d


def access_token():
    rt = ''
    with open(os.path.join(ROOT, 'env'), encoding='utf-8') as f:
        for line in f:
            if line.startswith('PIX_REFRESH_TOKEN='):
                rt = line.split('=', 1)[1].strip()
    if not rt:
        raise SystemExit('env 里没有 PIX_REFRESH_TOKEN')
    d = curl('https://oauth.secure.pixiv.net/auth/token',
             data=('client_id=%s&client_secret=%s&grant_type=refresh_token'
                   '&refresh_token=%s&include_policy=true&get_secure_url=1'
                   % (CLIENT_ID, CLIENT_SECRET, rt)))
    r = d.get('response') or {}
    if not r.get('access_token'):
        raise SystemExit('刷新 token 失败：%s' % json.dumps(d, ensure_ascii=False)[:300])
    if r.get('refresh_token') and r['refresh_token'] != rt:
        log('!! refresh_token 发生轮换，请更新 env')
    return r['access_token']


def keep(it):
    w, h = it.get('width') or 0, it.get('height') or 0
    if min(w, h) < MIN_SIDE:
        return False
    return (it.get('page_count') or 1) >= 1


def add(pool, it, src, taghit=0, artist=False):
    pid = str(it.get('id'))
    tags = [t.get('name', '') for t in (it.get('tags') or [])]
    rec = pool.get(pid)
    cur = {
        'pid': pid, 'uid': it['user']['id'], 'author': it['user']['name'],
        'title': it.get('title') or '', 'tags': tags,
        'bm': int(it.get('total_bookmarks') or 0),
        'view': int(it.get('total_view') or 0),
        'x': int(it.get('x_restrict') or 0) == 1,
        'ai': int(it.get('illust_ai_type') or 0) == 2,
        'pg': int(it.get('page_count') or 1),
        'w': int(it.get('width') or 0), 'h': int(it.get('height') or 0),
        'src': src, 'taghits': taghit, 'artist': artist,
    }
    if rec is None:
        pool[pid] = cur
        return
    rec['taghits'] = max(rec['taghits'], taghit)
    rec['artist'] = rec['artist'] or artist
    rec['bm'] = max(rec['bm'], cur['bm'])
    rec['view'] = max(rec['view'], cur['view'])


def search(at, word, hits_of):
    got, url, page = 0, None, 0
    q = ('https://app-api.pixiv.net/v1/search/illust?word=%s'
         '&search_target=partial_match_for_tags&sort=date_desc&filter=for_android'
         % __import__('urllib.parse', fromlist=['quote']).quote(word))
    while page < PAGES:
        d = curl(url or q, token=at)
        items = d.get('illusts') or []
        if not items:
            if d.get('error'):
                log('   !! %s -> %s' % (word, json.dumps(d.get('error'), ensure_ascii=False)[:120]))
            break
        for it in items:
            if keep(it):
                tags = [t.get('name', '') for t in (it.get('tags') or [])]
                hit = len(set(tags) & hits_of)
                add(POOL, it, 'tag', taghit=hit)
                got += 1
        url = d.get('next_url')
        page += 1
        if not url:
            break
        time.sleep(SLEEP)
    return got


def artist_works(at, uid):
    got, url, page = 0, None, 0
    q = ('https://app-api.pixiv.net/v1/user/illusts?user_id=%s&type=illust&filter=for_android'
         % uid)
    while page < ART_PAGES:
        d = curl(url or q, token=at)
        items = d.get('illusts') or []
        if not items:
            break
        for it in items:
            if keep(it):
                tags = [t.get('name', '') for t in (it.get('tags') or [])]
                add(POOL, it, 'artist', taghit=len(set(tags) & HITS), artist=True)
                got += 1
        url = d.get('next_url')
        page += 1
        if not url:
            break
        time.sleep(SLEEP)
    return got


db = json.load(open(DB2, encoding='utf-8'))
TAGS = []
for t in db['tags']:
    w = t.get('jp') if isinstance(t, dict) else t
    if isinstance(w, str) and w.strip():
        TAGS.append(w.strip())
HITS = set(TAGS)
ARTISTS = [(str(a['uid']), a.get('name', ''), float(a.get('star') or 3)) for a in db['artists']]

log('tag %d 个 / 画师 %d 位 / 每 tag %d 页' % (len(TAGS), len(ARTISTS), PAGES))
at = access_token()
log('access_token ok')

POOL = {}
t0 = time.time()
def dump(final=False):
    meta = {
        'built_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(POOL),
        'sfw': sum(1 for x in POOL.values() if not x['x']),
        'r18': sum(1 for x in POOL.values() if x['x']),
        'ai': sum(1 for x in POOL.values() if x['ai']),
        'artist': sum(1 for x in POOL.values() if x['artist']),
        'tags': len(TAGS), 'artists': len(ARTISTS), 'pages': PAGES,
        'source': 'pixiv app-api v1/search/illust + v1/user/illusts',
        'secs': round(time.time() - t0, 1), 'final': final,
    }
    tmp = OUT + '.tmp'
    json.dump(POOL, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
    os.replace(tmp, OUT)
    json.dump(meta, open(META, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log('DUMP %s count=%d sfw=%d r18=%d' % ('final' if final else 'partial',
        meta['count'], meta['sfw'], meta['r18']))


for i, tag in enumerate(TAGS, 1):
    n1 = search(at, tag, HITS)
    n2 = search(at, '%s R-18' % tag, HITS)
    log('[%3d/%d] %-22s safe+%d r18+%d 池=%d' % (i, len(TAGS), tag, n1, n2, len(POOL)))
    if DUMP_EVERY and i % DUMP_EVERY == 0:
        dump()

for i, (uid, name, star) in enumerate(ARTISTS, 1):
    n = artist_works(at, uid)
    log('[artist %2d/%d] %-18s ★%s +%d 池=%d' % (i, len(ARTISTS), name, star, n, len(POOL)))

if len(POOL) < int(os.environ.get('PIX_MIN_KEEP', '3000')):
    log('!! 只抓到 %d 条，疑似被限流，保留旧池子不覆盖' % len(POOL))
    sys.exit(3)

dump(final=True)
_UNUSED = {
    'built_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    'count': len(POOL),
    'sfw': sum(1 for x in POOL.values() if not x['x']),
    'r18': sum(1 for x in POOL.values() if x['x']),
    'ai': sum(1 for x in POOL.values() if x['ai']),
    'artist': sum(1 for x in POOL.values() if x['artist']),
    'tags': len(TAGS), 'artists': len(ARTISTS), 'pages': PAGES,
    'source': 'pixiv app-api v1/search/illust + v1/user/illusts',
    'secs': round(time.time() - t0, 1),
}
log('ALL DONE')
