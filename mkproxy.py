# -*- coding: utf-8 -*-
"""极简订阅→xray 配置（只为一个用途：临时开 socks5 10808 去连 pixiv 建池）。
子命令：
  fetch   拉订阅、解出节点、探测可用性、写 /opt/bh2-pix/proxy/config.json
  up      前台跑起来（systemd 用）
用法：python3 mkproxy.py fetch | up
"""
import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.environ.get('PIX_PROXY_ROOT') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'proxy')
SUB = os.path.join(ROOT, 'sub.url')
NODES = os.path.join(ROOT, 'nodes.json')
CONF = os.path.join(ROOT, 'config.json')
REPORT = os.path.join(ROOT, 'probe_report.json')
XRAY = os.environ.get('XRAY_BIN', '/usr/local/bin/xray')
SOCKS_PORT = int(os.environ.get('PIX_SOCKS_PORT', '10808'))
LATENCY_URL = 'https://www.gstatic.com/generate_204'
GOOGLE_URL = 'https://www.google.com/generate_204'
SKIP_WORDS = ('剩余流量', '套餐到期', '距离下次', '官网', '订阅', '重置', '过期')


def sub_url():
    with open(SUB) as f:
        return f.read().strip()


def parse_link(link):
    """vless://uuid@host:port?params#name → xray outbound"""
    if not link.startswith('vless://'):
        return None
    body = link[len('vless://'):]
    if '#' in body:
        body, name = body.split('#', 1)
        name = urllib.parse.unquote(name)
    else:
        name = ''
    uuid, rest = body.split('@', 1)
    hostport, _, qs = rest.partition('?')
    host, _, port = hostport.rpartition(':')
    host = host.strip('[]')
    q = dict(urllib.parse.parse_qsl(qs))
    net = q.get('type', 'tcp')
    sec = q.get('security', 'none')
    sni = q.get('sni') or q.get('host') or host
    stream = {'network': net, 'security': sec if sec in ('tls', 'reality') else 'none'}
    if sec == 'tls':
        stream['tlsSettings'] = {'serverName': sni, 'fingerprint': q.get('fp', 'chrome')}
    elif sec == 'reality':
        stream['realitySettings'] = {
            'serverName': sni,
            'fingerprint': q.get('fp', 'chrome'),
            'publicKey': q.get('pbk', ''),
            'shortId': q.get('sid', ''),
            'spiderX': q.get('spx', '/'),
        }
    if net == 'ws':
        stream['wsSettings'] = {'path': q.get('path', '/'),
                                'headers': {'Host': q.get('host', host)}}
    elif net == 'grpc':
        stream['grpcSettings'] = {'serviceName': q.get('serviceName', '')}
    elif net == 'tcp' and q.get('headerType') == 'http':
        stream['tcpSettings'] = {'header': {'type': 'http',
                                            'request': {'path': [q.get('path', '/')],
                                                        'headers': {'Host': [q.get('host', host)]}}}}
    tag = 'n%03d' % (abs(hash(name)) % 1000)
    return {
        'name': name, 'host': host, 'port': int(port),
        'out': {
            'tag': tag, 'protocol': 'vless', 'settings': {
                'vnext': [{'address': host, 'port': int(port),
                           'users': [{'id': uuid, 'encryption': 'none',
                                      'flow': q.get('flow', '')}]}]},
            'streamSettings': stream,
        },
    }


def cmd_fetch():
    os.makedirs(ROOT, exist_ok=True)
    raw = urllib.request.urlopen(sub_url(), timeout=25).read().decode('utf-8', 'replace')
    try:
        raw = base64.b64decode(raw + '=' * (-len(raw) % 4)).decode('utf-8', 'replace')
    except Exception:
        pass
    links = [l.strip() for l in raw.splitlines() if l.strip().startswith('vless://')]
    nodes = []
    for l in links:
        n = parse_link(l)
        if not n or any(w in n['name'] for w in SKIP_WORDS):
            continue
        nodes.append(n)
    print('订阅里 %d 条链接 → 可用节点 %d 个' % (len(links), len(nodes)))
    with open(NODES, 'w') as f:
        json.dump(nodes, f, ensure_ascii=False, indent=1)

    # 逐个探测：每个节点单独入站 + 单独路由（否则 xray 会把所有入站交给第一个出站）
    probe = {'log': {'loglevel': 'warning'}, 'inbounds': [], 'outbounds': [{'protocol': 'direct', 'tag': 'direct'}]}
    routes = []
    for i, n in enumerate(nodes[:40]):
        itag = 'in%03d' % i
        probe['inbounds'].append({'tag': itag, 'listen': '127.0.0.1', 'port': 12100 + i,
                                  'protocol': 'socks', 'settings': {'udp': False}})
        probe['outbounds'].append(n['out'])
        routes.append({'type': 'field', 'inboundTag': [itag], 'outboundTag': n['out']['tag']})
    probe['routing'] = {'domainStrategy': 'AsIs', 'rules': routes}
    pconf = os.path.join(ROOT, 'probe.json')
    with open(pconf, 'w') as f:
        json.dump(probe, f, ensure_ascii=False, indent=1)

    xlog = open('/tmp/xray-probe.log', 'w')
    proc = subprocess.Popen([XRAY, 'run', '-c', pconf], stdout=xlog, stderr=xlog)
    time.sleep(3)
    good = []
    try:
        for i, n in enumerate(nodes[:40]):
            port = 12100 + i
            ok, ms = False, None
            for url in (GOOGLE_URL, LATENCY_URL):
                t0 = time.time()
                r = subprocess.run(['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}',
                                    '-m', '12', '--socks5-hostname', '127.0.0.1:%d' % port, url],
                                   capture_output=True, text=True)
                code = (r.stdout or '').strip()
                if code in ('204', '200'):
                    ok, ms = True, int((time.time() - t0) * 1000)
                    break
            print('  %-28s %s%s' % (n['name'][:26], 'OK' if ok else '--',
                                    '  %dms' % ms if ms else ''))
            if ok:
                good.append({'name': n['name'], 'ms': ms, 'out': n['out']})
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    good.sort(key=lambda x: x['ms'])
    with open(REPORT, 'w') as f:
        json.dump([{'name': g['name'], 'ms': g['ms']} for g in good], f, ensure_ascii=False, indent=1)
    if not good:
        print('!! 没有可用节点')
        try:
            print(open('/tmp/xray-probe.log', encoding='utf-8', errors='replace').read()[-900:])
        except OSError:
            pass
        return 1
    best = good[0]
    conf = {
        'log': {'loglevel': 'warning'},
        'inbounds': [{'tag': 'socks-in', 'listen': '127.0.0.1', 'port': SOCKS_PORT,
                      'protocol': 'socks', 'settings': {'udp': False}}],
        'outbounds': [best['out'], {'protocol': 'direct', 'tag': 'direct'}],
        'routing': {'domainStrategy': 'AsIs',
                    'rules': [{'type': 'field', 'inboundTag': ['socks-in'],
                               'outboundTag': best['out']['tag']}]},
    }
    with open(CONF, 'w') as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)
    print('选定节点：%s（%dms）→ %s' % (best['name'], best['ms'], CONF))
    return 0


def cmd_up():
    os.execv(XRAY, [XRAY, 'run', '-c', CONF])


if __name__ == '__main__':
    what = sys.argv[1] if len(sys.argv) > 1 else 'fetch'
    sys.exit(cmd_fetch() if what == 'fetch' else cmd_up())
