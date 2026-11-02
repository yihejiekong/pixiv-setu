# -*- coding: utf-8 -*-
"""来点二次元 / 来点色图 / 来点黑丝色图 —— 按第 2 批 Pixiv 数据库 + 加分制出图。

语义：
    来点二次元            -> 非 R18
    来点色图              -> R18
    来点黑丝色图          -> 模糊词「黑丝」命中的 tag 的 R18 图
    来点女仆装二次元      -> 模糊词「女仆装」命中的 tag 的非 R18 图

出图流程（应对 QQ 吞图）：
    1) 先单独发图片，再单独发文字信息 —— 图被吞时文字仍可见
    2) 10 分钟内有人说「图被吞了 / 图没了 / 图呢」，用合并转发补发刚发过的那张
打分在 pixcli.py：S2 质量分 > S1 偏好命中 > S3 画师，取总分前 30% 均匀随机；
同一群 7 天内发过的作品不重复。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from collections import deque

from nonebot import logger, on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot, GroupMessageEvent, MessageEvent, MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.rule import Rule
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import Message

from src.plugins import _config as _cfg
from src.plugins import module_switch as _switch

MODULE = "pixiv"
PIX_ROOT = os.environ.get("PIX_ROOT", "/opt/pixiv-setu")
CLI = os.path.join(PIX_ROOT, "pixcli.py")
PY = sys.executable
TIMEOUT = 120
COOLDOWN_SEC = 15
CACHE = str(_cfg.MATERIAL_ROOT / "随机色图")
KEEP = 80
DEDUP_DAYS = 7
EATEN_WINDOW = 900          # 补发窗口：15 分钟
EATEN_KEEP = 6              # 每组保留最近 6 张备查

_LOCK = asyncio.Semaphore(1)
_LAST: dict = {}
_SENT: dict = {}            # group_id -> deque[{pid, file, caption, ts, uid}]

PREFIX = ("给我来点", "给我来张", "来点", "来张", "整点", "我要", "求")
KIND_LEWD = re.compile(r"(色图|涩图|瑟图|r18|R18|R-18)")
KIND_ANIME = re.compile(r"(二次元|二次源|二次)")
NOISE = re.compile(r"(色图|涩图|瑟图|二次元|二次源|r18|R18|R-18|一张|张|的|图)")
LEGACY = {
    "色图": ("", "lewd"), "随机色图": ("", "lewd"), "来张色图": ("", "lewd"),
    "二次元": ("", "anime"), "随机二次元": ("", "anime"),
}
EATEN = re.compile(r"(图.{0,3}(被吞|没了|没发|挂了|看不到|看不见|没出|不出来|在哪|呢)|"
                   r"吞了|被吞|看不到图|没图|图片.{0,3}(没|不))")


def _db():
    conn = sqlite3.connect(str(_cfg.DB), timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS pix_sent(
        group_id INTEGER NOT NULL, pid TEXT NOT NULL, ts INTEGER NOT NULL,
        PRIMARY KEY(group_id, pid))""")
    conn.commit()
    return conn


def _recent(gid: int) -> list:
    if not gid:
        return []
    cut = int(time.time()) - DEDUP_DAYS * 86400
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT pid FROM pix_sent WHERE group_id=? AND ts>=?", (gid, cut)).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _mark(gid: int, pid: str):
    if not gid or not pid:
        return
    try:
        with _db() as conn:
            conn.execute("INSERT OR REPLACE INTO pix_sent(group_id,pid,ts) VALUES(?,?,?)",
                         (gid, str(pid), int(time.time())))
            conn.execute("DELETE FROM pix_sent WHERE ts<?", (int(time.time()) - 60 * 86400,))
    except Exception:
        pass


def _remember(gid: int, d: dict, caption: str, uid: str):
    if not gid:
        return
    box = _SENT.setdefault(gid, deque(maxlen=EATEN_KEEP))
    box.append({'pid': d.get('pid'), 'file': d.get('file'), 'caption': caption,
                'ts': time.time(), 'uid': uid, 'pid_raw': d.get('pid')})


def _last_sent(gid: int):
    box = _SENT.get(gid)
    if not box:
        return None
    cut = time.time() - EATEN_WINDOW
    for it in reversed(box):
        if it['ts'] >= cut and os.path.exists(it['file'] or ''):
            return it
    return None


def _admin(event) -> bool:
    admins = getattr(_cfg, "ADMIN_SET", None) or getattr(_switch, "_ADMIN_USERS", set()) or set()
    try:
        return str(event.get_user_id()) in {str(x) for x in admins}
    except Exception:
        return False


def _parse(text: str):
    """-> (模糊词, 'lewd'|'anime') 或 None"""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t or len(t) > 24:
        return None
    if t in LEGACY:
        return LEGACY[t]
    if EATEN.fullmatch(t):
        return None
    if not t.startswith(PREFIX):
        return None
    core = t
    for p in sorted(PREFIX, key=len, reverse=True):
        if core.startswith(p):
            core = core[len(p):]
            break
    if KIND_LEWD.search(core):
        kind = "lewd"
    elif KIND_ANIME.search(core):
        kind = "anime"
    else:
        kind = "anime"
    mid = NOISE.sub("", core).strip()
    if len(mid) > 16:
        return None
    return mid, kind


def _rule(event: MessageEvent) -> bool:
    return _parse(event.get_plaintext()) is not None


def _is_eaten(event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False
    t = event.get_plaintext().strip()
    return 0 < len(t) <= 20 and bool(EATEN.search(t))


async def _run_cli(word: str, kind: str, exclude=None, timeout=TIMEOUT, artist=None):
    os.makedirs(CACHE, exist_ok=True)
    tag = "artist" if artist else kind
    out = os.path.join(CACHE, "pix_%s_%d.jpg" % (tag, int(time.time() * 1000)))
    cmd = [PY, CLI, "pick", "--mode", kind, "--out", out]
    if artist:
        cmd += ["--artist", artist]
    elif word:
        cmd += ["--fuzzy", word]
    if exclude:
        cmd += ["--exclude", ",".join(str(x) for x in exclude[:600])]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        so, se = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "reason": "timeout"}
    txt = (so or b"").decode("utf-8", "replace")
    for line in reversed(txt.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return {"ok": False, "reason": "bad_output", "raw": txt[-200:]}


def _caption(d) -> str:
    name = d.get("author") or "未知画师"
    head = name
    try:
        bm = int(d.get("bm") or 0)
        view = int(d.get("view") or 0)
    except Exception:
        bm = view = 0
    if bm or view:
        head += " · ♥%s/👁%s" % (bm, view)
    if d.get("artist_name"):
        head = "%s（画师模式 · %s/%s 张）" % (name, d.get("rank"), d.get("kept"))
    hits = d.get("hit_tags") or []
    if hits:
        head += " · " + "、".join(hits[:2])
    flags = (["R18"] if d.get("r18") else []) + (["AI"] if d.get("ai") else [])
    if flags:
        head += " · " + "/".join(flags)
    return head + "\n没看到图就回「图被吞了」"


def _reencode(path: str) -> str | None:
    """重新编码一张图（轻微缩放 + 2px 白边 + 去元数据），用来绕开按图匹配的拦截。"""
    try:
        from PIL import Image
        out = os.path.join(CACHE, "re_%s.jpg" % os.path.basename(path).rsplit('.', 1)[0])
        im = Image.open(path)
        im = im.convert('RGB')
        w, h = im.size
        nw, nh = max(1, int(w * 0.995)), max(1, int(h * 0.995))
        im = im.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new('RGB', (nw + 4, nh + 4), (255, 255, 255))
        canvas.paste(im, (2, 2))
        canvas.save(out, 'JPEG', quality=92, optimize=True, progressive=True)
        return out if os.path.exists(out) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("pixiv 重新编码失败：%s" % e)
        return None


async def _try_send_image(matcher, path: str) -> bool:
    """发一张图，失败就重新编码后再发一次。"""
    try:
        await matcher.send(MessageSegment.image(path))
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("pixiv 发图被拒（图1）：%s" % e)
    alt = _reencode(path)
    if alt:
        try:
            await matcher.send(MessageSegment.image(alt))
            logger.info("pixiv 重新编码后的图发送成功")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("pixiv 重新编码后仍被拒：%s" % e)
    return False


def _cleanup(keep=KEEP):
    try:
        files = [os.path.join(CACHE, f) for f in os.listdir(CACHE) if f.startswith("pix_")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for p in files[keep:]:
            os.remove(p)
    except Exception:
        pass


pix = on_message(rule=Rule(_rule), priority=6, block=True)


@pix.handle()
async def _(event: MessageEvent):
    word, kind = _parse(event.get_plaintext())
    gid = event.group_id if isinstance(event, GroupMessageEvent) else 0
    if isinstance(event, GroupMessageEvent):
        if not await _switch.require(pix, event, MODULE):
            return
    uid = str(event.get_user_id())
    now = time.time()
    last = _LAST.get(uid, 0)
    if now - last < COOLDOWN_SEC:
        await pix.finish("冷却中，%d 秒后再来" % int(COOLDOWN_SEC - (now - last)), at_sender=True)
        return
    _LAST[uid] = now
    exclude = _recent(gid)
    async with _LOCK:
        d = await _run_cli(word, kind, exclude)
    if not d.get("ok") and d.get("reason") == "unknown_word" and word:
        # tag 没命中就当作画师名再试一次（来点鬼针 -> 鬼针草）
        async with _LOCK:
            d2 = await _run_cli("", kind, exclude, artist=word)
        if d2.get("ok"):
            d = d2
        elif d2.get("reason") == "unknown_artist":
            arts = " / ".join([str(x) for x in (d2.get("suggest") or [])][:6])
            await pix.finish("「%s」既不是偏好 tag 也不是候选画师。\n发「画师列表」「tag列表」看能用的名字"
                             % word, at_sender=True)
            return
        else:
            d = d2
    if not d.get("ok"):
        reason = d.get("reason")
        if reason == "unknown_word":
            sug = " / ".join(d.get("suggest") or []) or "黑丝 / 白丝 / 女仆装 / 过膝袜"
            await pix.finish("没找到「%s」对应的 tag。可以试试：%s" % (word, sug), at_sender=True)
            return
        if reason == "artist_empty":
            await pix.finish("「%s」名下没有能用的作品（可能全被 7 天去重挡掉了）" % word,
                             at_sender=True)
            return
        if reason in ("no_token", "artist_failed"):
            await pix.finish("拉画师作品失败（%s），稍后再试" % reason, at_sender=True)
            return
        if reason == "empty":
            if kind == "lewd":
                await pix.finish("池子里这个范围内暂时没有 R18 素材，等下次建池补上", at_sender=True)
                return
            await pix.finish("这个组合暂时没货（%s）" % (word or "默认范围"), at_sender=True)
            return
        if reason == "no_pool":
            await pix.finish("色图池还没建好，稍后再试", at_sender=True)
            return
        await pix.finish("出图失败：%s" % reason, at_sender=True)
        return
    path = d.get("file")
    if not path or not os.path.exists(path):
        await pix.finish("出图失败：文件不存在", at_sender=True)
        return
    _mark(gid, d.get("pid"))
    caption = _caption(d)
    _remember(gid, d, caption, uid)
    _cleanup()
    # 图、文分开发：图被 QQ 吞掉时文字仍在，用户能判断是"被吞"而不是"没出图"
    ok = await _try_send_image(pix, path)
    text = caption if ok else caption + "\n（图被吞了 —— 回「图被吞了」，我用合并转发重发）"
    try:
        await pix.send(MessageSegment.text(text))
    except Exception as e:
        logger.warning("pixiv 发文字也失败：%s" % e)


eaten = on_message(rule=Rule(_is_eaten), priority=5, block=False)


@eaten.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    it = _last_sent(event.group_id)
    if not it:
        await eaten.finish("近 15 分钟内没有可补发的图（或图已被清理）", at_sender=True)
        return
    path = it["file"]
    sid = str(bot.self_id)
    node = {"type": "node", "data": {
        "name": "色图补发", "uin": sid,
        "nickname": "色图补发", "user_id": sid,
        "content": [
            {"type": "image", "data": {"file": path}},
            {"type": "text", "data": {"text": it["caption"]}},
        ],
    }}
    forward_err = None
    try:
        await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=[node])
        await eaten.finish()
        return
    except Exception as e:  # noqa: BLE001
        forward_err = e
        logger.warning("pixiv 合并转发补发失败：%s" % e)

    # 二级：重新编码后直发
    alt = _reencode(path)
    for cand in [p for p in (alt, path) if p]:
        try:
            await eaten.send(MessageSegment.image(cand))
            await eaten.finish(MessageSegment.text(it["caption"]))
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("pixiv 补发直发失败（%s）：%s" % (os.path.basename(cand), e))

    # 三级：当群文件发（文件不走图片审核）
    try:
        await bot.call_api("upload_group_file", group_id=event.group_id,
                           file=path, name=os.path.basename(path))
        await eaten.finish(MessageSegment.text("图被 QQ 拦了，已按文件发到群文件：%s\n%s"
                                               % (os.path.basename(path), it["caption"])))
        return
    except Exception as e:  # noqa: BLE001
        logger.warning("pixiv 群文件补发也失败：%s" % e)

    await eaten.finish(MessageSegment.text(
        "这张补发不出来（QQ 三次都拒收：转发/直发/群文件）。\n换一张就好，池子里还有别的。\n%s"
        % it["caption"]), at_sender=True)


status = on_command("色图状态", priority=5, block=True)


@status.handle()
async def _(event: MessageEvent):
    if not _admin(event):
        await status.finish()
    proc = await asyncio.create_subprocess_exec(
        PY, CLI, "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    so, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    try:
        m = json.loads(so.decode("utf-8", "replace")) or {}
    except Exception:
        m = {}
    try:
        files = len([f for f in os.listdir(CACHE) if f.startswith("pix_")])
    except Exception:
        files = 0
    await status.finish("池子 %s 条（非R18 %s / R18 %s）\nAI %s · 画师 %s · Tag %s · 每 tag %s 页\n建池 %s · 缓存 %d 张" % (
        m.get("count"), m.get("sfw"), m.get("r18"), m.get("ai"),
        m.get("artists"), m.get("tags"), m.get("pages"), m.get("built_at"), files))


rebuild = on_command("重建色图池", priority=5, block=True)


@rebuild.handle()
async def _(event: MessageEvent):
    if not _admin(event):
        await rebuild.finish()
    await rebuild.send("开始重建，约 20~25 分钟（App API 拉 163 个 tag + 82 位画师）")
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", "systemctl", "start", "--no-block",
        os.environ.get("PIX_BUILD_UNIT", "pixiv-pool-build.service"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, se = await asyncio.wait_for(proc.communicate(), timeout=60)
    err = (se or b"").decode("utf-8", "replace").strip()[:200]
    await rebuild.finish("已启动" if proc.returncode == 0 else "重建启动失败：%s" % err)



artist_list = on_command("画师列表", aliases={"画师池", "作者列表"}, priority=5, block=True)
tag_list = on_command("tag列表", aliases={"标签列表", "tag池", "词表"}, priority=5, block=True)


async def _cli_json(*args, timeout=45):
    proc = await asyncio.create_subprocess_exec(
        PY, CLI, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    so, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    try:
        return json.loads((so or b"").decode("utf-8", "replace"))
    except Exception:
        return {}


@artist_list.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    kw = arg.extract_plain_text().strip()
    extra = ["--keyword", kw] if kw else []
    d = await _cli_json("artists", *extra)
    rows = d.get("artists") or []
    if not rows:
        await artist_list.finish("没找到画师%s" % ("「%s」" % kw if kw else ""), at_sender=True)
        return
    lines, cur, buf = [], None, []
    for r in rows[:48]:
        star = "★%d" % int(float(r.get("star") or 3))
        if star != cur:
            if buf:
                lines.append("%s  %s" % (cur, " / ".join(buf)))
            cur, buf = star, []
        buf.append(str(r.get("name")))
    if buf:
        lines.append("%s  %s" % (cur, " / ".join(buf)))
    tail = "\n用法：来点<画师名>，支持模糊（来点鬼针 → 鬼针草）"
    if d.get("total", 0) > 48:
        tail += "\n共 %d 位，找特定画师发「画师列表 关键词」" % d.get("total")
    await artist_list.finish("🎨 候选画师 %d 位\n%s%s" % (min(48, len(rows)), "\n".join(lines), tail),
                             at_sender=True)


@tag_list.handle()
async def _(event: MessageEvent, arg: Message = CommandArg()):
    kw = arg.extract_plain_text().strip()
    extra = ["--keyword", kw] if kw else []
    d = await _cli_json("tags", *extra)
    rows = d.get("tags") or []
    if not rows:
        await tag_list.finish("没找到 tag%s" % ("「%s」" % kw if kw else ""), at_sender=True)
        return
    show = rows[:40]
    cells = []
    for r in show:
        cn = r.get("cn") or ""
        cells.append("%s%s" % (r.get("jp"), "（%s）" % cn if cn else ""))
    lines = [" · ".join(cells[i:i + 2]) for i in range(0, len(cells), 2)]
    tail = "\n用法：来点<词>色图 / 来点<词>二次元，中文/别名/日文都认"
    if d.get("total", 0) > len(show):
        tail += "\n共 %s 个，筛选发「tag列表 关键词」" % d.get("total")
    await tag_list.finish("🏷 偏好 tag %d 个\n%s%s" % (len(show), "\n".join(lines), tail),
                          at_sender=True)