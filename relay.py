#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webrc 中继：放在有公网 IP 的机器上，只做转发，不解码画面。

  被控电脑 A 的 agent.py ─┐
  被控电脑 B 的 agent.py ─┴─(出站 WSS /agent?name=…)─▶ relay ◀─(WSS /ws?pc=…)── 浏览器（电脑 / 手机）

一个中继可以同时挂多台被控电脑：每台一个名字（agent 的 --name，默认本机 hostname），中继按名字各开一路
（Hub：一个 agent + 它的观众 + 它的关键帧缓存 + 它的流控），互不干扰。浏览器工具栏上的「电脑」下拉框随时切，
切换只是把这个观众从一路挪到另一路，WebSocket 不用重连。同名的 agent 再连进来会顶掉旧的（和以前一样）。

用法：
  python relay.py --port 33445 --token <随机串> --cert relay.crt --key relay.key [--log relay.log]
  --token 省略时自动生成并写到同目录 relay.token，下次沿用；
  --cert/--key 省略则明文 http/ws（只适合内网联调）。
  浏览器打开 https://<公网IP>:33445/?token=<随机串>

协议（浏览器与 agent 之间的消息原样转发，relay 只认下面几种）：
  agent   → relay : 二进制帧（jpeg 条带 / h264 访问单元）→ 发给这一路的所有观众，不丢帧（增量帧丢了画面就错了）
  relay   → agent : {"t":"ack"}   一帧被所有观众看完后回执，agent 据此节流，慢链路上不会堆积
                    {"t":"kf"}    要一张关键帧（和键盘 {"t":"key"} 不是一回事）——只在缓存里没有可回放的关键帧时才发
                    {"t":"viewers","n":N}  在线观众数，0 时 agent 停止截屏
                    {"t":"release"}        有观众断开或切走时让 agent 松开所有按住的键
  浏览器 → relay : {"t":"ack"}    看完一帧回执；{"t":"ping","ts":..} relay 直接回 pong；
                    {"t":"kf"}     解不出来/刚进来要关键帧：relay 有缓存就直接回放，没有才转给 agent；
                    {"t":"pick","pc":"名字"}  切到另一台被控电脑，relay 自己消化，不转给 agent；其他原样转给 agent
  relay   → 浏览器: {"t":"pcs","list":[{"name":…,"online":…,"viewers":N},…]}  被控电脑列表，有增减就广播给所有观众
                    {"t":"pc","name":…}    你现在看的是哪台（连上时、切换后各发一次；页面收到要把解码器清掉重来）
                    {"t":"agent","online":…}  你正在看的这台在不在线
  观众超过 STALL_TIMEOUT 秒不回执就被踢掉重连（比如手机切后台），免得拖住别人。

关键帧缓存：relay 只看每帧头里的 flags（bit0 = 关键帧），每一路各记住最近一个关键帧起的所有帧。新观众进来（或从别的
  机器切过来）直接把这串回放给他，agent 不用重新出关键帧（h264 下出关键帧要重开 ffmpeg，约 1 秒；而且会打断其他
  观众的画面）。缓存超过 CACHE_FRAMES 帧或 CACHE_BYTES 就作废，等 agent 下一个关键帧再开始记。
"""
import argparse
import asyncio
import json
import logging
import queue
import re
import secrets
import ssl
import sys
import threading
import time
from collections import deque
from pathlib import Path

from aiohttp import web, WSMsgType

HERE = Path(__file__).resolve().parent
STALL_TIMEOUT = 6.0
CACHE_FRAMES = 200
# 回放缓存上限。新观众要把整串收完才追上实时画面：出口几 Mbps 时 1 MB 就是 2–3 秒，再大还不如让 agent 重出一个
# 关键帧（h264 约 1 秒）；回放超过 STALL_TIMEOUT 还会被当成卡住踢掉、重连又回放，死循环（原来 8 MB）
CACHE_BYTES = 1 << 20
REPLAY_MIN_GAP = 2.0      # 同一观众两次回放至少隔这么久；更频繁的 kf 说明缓存本身解不了，转给 agent 要新的
DEFAULT_NAME = "unnamed"  # 没给 --name 的老 agent 挂在这个名字下
NAME_MAX = 32
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f]")
log = logging.getLogger("relay")


def clean_name(raw):
    """被控端名字来自 URL，当字典键和页面文字用，去掉控制字符再截断。"""
    return _BAD_NAME.sub("", (raw or "").strip())[:NAME_MAX] or DEFAULT_NAME


async def send_to(ws, data):
    try:
        if isinstance(data, (bytes, bytearray)):
            await ws.send_bytes(data)
        else:
            await ws.send_str(data)
    except Exception:
        pass  # 单个连接坏了不影响别人，它自己的 handler 会收尾


class Viewer:
    __slots__ = ("ws", "peer", "unacked", "queue", "last_replay", "hub")

    def __init__(self, ws, peer):
        self.ws = ws
        self.peer = peer
        self.unacked = deque()         # (frame_id 或 None（回放的缓存帧）, sent_at)，按顺序回执
        self.queue = asyncio.Queue()   # 发给这个观众的消息队列，一个任务顺序发，保证帧序
        self.last_replay = 0.0
        self.hub = None                # 当前挂在哪一路

    def drain(self):
        """切换被控电脑时丢掉队列里还没发出去的旧机器的帧，免得画到新机器的画布上。"""
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self.unacked.clear()
        self.last_replay = 0.0   # 马上要回放新机器的缓存，别被上一次回放的冷却挡住


async def pump(v):
    """一个观众一个发送任务：队列里什么顺序进，就什么顺序发。"""
    while True:
        data = await v.queue.get()
        if data is None:
            return
        await send_to(v.ws, data)


class Hub:
    """一台被控电脑：它的 agent、看它的观众、它的关键帧缓存和流控，各路之间互不影响。"""

    def __init__(self, name):
        self.name = name
        self.agent = None       # 被控端的 WebSocketResponse，同一个名字同一时刻只认一个
        self.viewers = set()
        self.frames = {}        # frame_id -> 还没回执的观众集合
        self.next_id = 0
        self.cache = []         # 最近一个关键帧起的所有帧
        self.cache_bytes = 0
        self.n_frames = 0
        self.n_bytes = 0
        self.t_stat = time.monotonic()

    def broadcast(self, text):
        for v in self.viewers:
            v.queue.put_nowait(text)

    async def to_agent(self, text):
        if self.agent is not None:
            await send_to(self.agent, text)

    def ack_agent(self):
        asyncio.create_task(self.to_agent('{"t":"ack"}'))

    async def viewers_changed(self):
        await self.to_agent(json.dumps({"t": "viewers", "n": len(self.viewers)}))

    def _remember(self, data):
        key = len(data) >= 12 and data[8] & 1
        if key:
            self.cache = [data]
            self.cache_bytes = len(data)
        elif self.cache:
            self.cache.append(data)
            self.cache_bytes += len(data)
            if len(self.cache) > CACHE_FRAMES or self.cache_bytes > CACHE_BYTES:
                self.cache = []
                self.cache_bytes = 0

    def on_frame(self, data):
        self.n_frames += 1
        self.n_bytes += len(data)
        self._remember(data)
        if not self.viewers:
            self.ack_agent()
            return
        fid = self.next_id
        self.next_id += 1
        self.frames[fid] = set(self.viewers)
        now = time.monotonic()
        for v in self.viewers:
            v.unacked.append((fid, now))
            v.queue.put_nowait(data)

    def replay(self, v):
        """把缓存回放给这个观众。返回 False 表示没得回放（或刚回放过），调用方应转而向 agent 要关键帧。"""
        now = time.monotonic()
        if not self.cache or now - v.last_replay < REPLAY_MIN_GAP:
            return False
        v.last_replay = now
        for data in self.cache:
            v.unacked.append((None, now))
            v.queue.put_nowait(data)
        log.info("[%s] replay %d frames (%d KB) -> %s", self.name, len(self.cache), self.cache_bytes // 1024, v.peer)
        return True

    def _settle(self, fid, v):
        waiting = self.frames.get(fid)
        if waiting is None:
            return
        waiting.discard(v)
        if not waiting:
            del self.frames[fid]
            self.ack_agent()

    def on_ack(self, v):
        if v.unacked:
            fid, _ = v.unacked.popleft()
            if fid is not None:
                self._settle(fid, v)

    def drop_viewer(self, v):
        """把观众从这一路摘掉（断开或切走）。不动它的发送任务，那个由 handler 收尾。"""
        self.viewers.discard(v)
        for fid, _ in list(v.unacked):
            if fid is not None:
                self._settle(fid, v)
        v.unacked.clear()


class Registry:
    """所有被控电脑，按名字索引。既没有 agent 又没有观众的那一路会被删掉，不会越攒越多。"""

    def __init__(self):
        self.hubs = {}

    def hub(self, name):
        h = self.hubs.get(name)
        if h is None:
            h = self.hubs[name] = Hub(name)
        return h

    def prune(self, h):
        if h.agent is None and not h.viewers and self.hubs.get(h.name) is h:
            del self.hubs[h.name]

    def default_name(self):
        """网址里没写 pc 的观众（老页面、第一次打开）挂到哪台：优先第一台在线的。"""
        names = sorted(self.hubs)
        for name in names:
            if self.hubs[name].agent is not None:
                return name
        return names[0] if names else DEFAULT_NAME

    def list_msg(self):
        return json.dumps({"t": "pcs", "list": [
            {"name": n, "online": h.agent is not None, "viewers": len(h.viewers)}
            for n, h in sorted(self.hubs.items())]})

    def broadcast_list(self):
        msg = self.list_msg()
        for h in self.hubs.values():
            h.broadcast(msg)

    async def attach(self, v, h):
        v.hub = h
        h.viewers.add(v)
        v.queue.put_nowait(json.dumps({"t": "pc", "name": h.name}))
        v.queue.put_nowait(json.dumps({"t": "agent", "online": h.agent is not None}))
        await h.viewers_changed()
        if not h.replay(v):
            await h.to_agent('{"t":"kf"}')

    async def detach(self, v):
        h, v.hub = v.hub, None
        if h is None:
            return
        h.drop_viewer(v)
        await h.to_agent('{"t":"release"}')   # 走了就把它按住的键松开，别粘在那台机器上
        await h.viewers_changed()
        self.prune(h)

    async def switch(self, v, raw):
        name = clean_name(raw)
        if v.hub is not None and v.hub.name == name:
            return
        log.info("viewer %s: [%s] -> [%s]", v.peer, v.hub.name if v.hub else "-", name)
        await self.detach(v)
        v.drain()
        await self.attach(v, self.hub(name))
        self.broadcast_list()


def check_token(request):
    expected = request.app["token"]
    supplied = request.query.get("token", "")
    if not expected or not secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise web.HTTPForbidden(text="bad token")


async def index(request):
    check_token(request)
    return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


async def viewer_ws(request):
    check_token(request)
    reg = request.app["reg"]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)
    v = Viewer(ws, request.remote)
    task = asyncio.create_task(pump(v))
    want = request.query.get("pc")
    name = clean_name(want) if want else reg.default_name()
    await reg.attach(v, reg.hub(name))
    reg.broadcast_list()
    log.info("viewer + %s -> [%s] (now %d)", v.peer, name, len(v.hub.viewers))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                ev = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            t = ev.get("t")
            hub = v.hub
            if hub is None:
                break
            if t == "ack":
                hub.on_ack(v)
            elif t == "ping":
                v.queue.put_nowait(json.dumps({"t": "pong", "ts": ev.get("ts")}))
            elif t == "pick":
                requested = ev.get("pc")
                if requested is None or isinstance(requested, str):
                    await reg.switch(v, requested)
            elif t == "kf":
                if not hub.replay(v):
                    await hub.to_agent(msg.data)
            elif t == "viewers":
                continue  # Viewer counts are owned by the relay, never by a viewer.
            else:
                await hub.to_agent(msg.data)
    finally:
        peer, where = v.peer, v.hub.name if v.hub else "-"
        await reg.detach(v)
        task.cancel()
        reg.broadcast_list()
        log.info("viewer - %s [%s]", peer, where)
    return ws


async def agent_ws(request):
    check_token(request)
    reg = request.app["reg"]
    name = clean_name(request.query.get("name"))
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 << 20)
    await ws.prepare(request)
    hub = reg.hub(name)
    old, hub.agent = hub.agent, ws
    if old is not None:
        log.info("[%s] agent replaced, closing old one", name)
        asyncio.create_task(old.close(code=4000, message=b"replaced"))
    hub.frames.clear()
    hub.cache = []
    hub.cache_bytes = 0
    for v in hub.viewers:
        v.unacked.clear()
    log.info("[%s] agent + %s", name, request.remote)
    hub.broadcast(json.dumps({"t": "agent", "online": True}))
    reg.broadcast_list()
    await hub.viewers_changed()
    if hub.viewers:
        await hub.to_agent('{"t":"kf"}')
    try:
        async for msg in ws:
            if hub.agent is not ws:
                break  # A replacement connection now owns this PC's stream.
            if msg.type == WSMsgType.BINARY:
                hub.on_frame(msg.data)
            elif msg.type == WSMsgType.TEXT:
                hub.broadcast(msg.data)
    finally:
        if hub.agent is ws:
            hub.agent = None
            hub.broadcast(json.dumps({"t": "agent", "online": False}))
            reg.prune(hub)
            reg.broadcast_list()
        log.info("[%s] agent - %s", name, request.remote)
    return ws


async def watchdog(app):
    """踢掉长时间不回执的观众；每 10 秒把有流量的那几路各记一行统计。"""
    reg = app["reg"]
    try:
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for hub in list(reg.hubs.values()):
                for v in list(hub.viewers):
                    if v.unacked and now - v.unacked[0][1] > STALL_TIMEOUT:
                        log.warning("[%s] viewer %s stalled (%d frames unacked), closing", hub.name, v.peer, len(v.unacked))
                        await reg.detach(v)
                        reg.broadcast_list()
                        asyncio.create_task(v.ws.close(code=4001, message=b"stalled"))
                if now - hub.t_stat >= 10:
                    dt = now - hub.t_stat
                    if hub.n_frames:
                        log.info("[%s] %.1f fps  %.0f KB/s  viewers=%d  cache=%d frames/%d KB", hub.name,
                                 hub.n_frames / dt, hub.n_bytes / 1024 / dt,
                                 len(hub.viewers), len(hub.cache), hub.cache_bytes // 1024)
                    hub.n_frames = hub.n_bytes = 0
                    hub.t_stat = now
    except asyncio.CancelledError:
        pass


async def start_watchdog(app):
    app["watchdog"] = asyncio.create_task(watchdog(app))


async def stop_watchdog(app):
    app["watchdog"].cancel()
    await app["watchdog"]


def disable_quick_edit():
    """Windows：关掉本进程控制台的「快速编辑」。开着的话，远程桌面上在中继的控制台窗口里点一下就进入选择状态，
    中继下一次写日志就卡死，所有被控端和观众一起断（agent 那边 2026-09-24 真出过同样的事）。"""
    if sys.platform != "win32":
        return
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = ctypes.c_void_p
    k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    k32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    h = k32.GetStdHandle(-10)          # STD_INPUT_HANDLE
    mode = ctypes.c_uint()
    if h and k32.GetConsoleMode(h, ctypes.byref(mode)):
        k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)   # 去掉 ENABLE_QUICK_EDIT_MODE，带上 ENABLE_EXTENDED_FLAGS


class ConsoleHandler(logging.Handler):
    """日志写控制台放到后台线程：控制台写不动（被选中、被暂停）时卡住的只是这个线程，转发照常；积压太多就丢，文件日志不受影响。"""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.q = queue.Queue(maxsize=2000)
        threading.Thread(target=self._drain, daemon=True, name="console-log").start()

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except queue.Full:
            pass
        except Exception:
            self.handleError(record)

    def _drain(self):
        while True:
            line = self.q.get()
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except Exception:
                pass


def load_token(given):
    if given is not None:
        if not given.strip():
            raise ValueError("--token must not be empty or whitespace")
        return given
    f = HERE / "relay.token"
    if f.exists():
        tok = f.read_text(encoding="utf-8").strip()
        if not tok:
            raise ValueError("relay.token is empty; set a non-empty token before starting the relay")
        return tok
    tok = secrets.token_urlsafe(18)
    f.write_text(tok, encoding="utf-8")
    log.info("generated token -> %s", f)
    return tok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=33445)
    ap.add_argument("--token", help="访问口令；省略则读/生成 relay.token")
    ap.add_argument("--cert", help="TLS 证书 PEM；给了就走 https/wss")
    ap.add_argument("--key", help="TLS 私钥 PEM（省略则认为和 --cert 是同一个文件）")
    ap.add_argument("--log", help="日志文件（计划任务里无控制台时用）")
    args = ap.parse_args()

    handlers = []
    if sys.stdout is not None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        disable_quick_edit()
        handlers.append(ConsoleHandler(sys.stdout))
    if args.log or sys.stdout is None:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(args.log or str(HERE / "relay.log"), maxBytes=2_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%m-%d %H:%M:%S", handlers=handlers)

    ssl_ctx = None
    if args.cert:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(args.cert, args.key or args.cert)

    app = web.Application()
    try:
        app["token"] = load_token(args.token)
    except ValueError as exc:
        ap.error(str(exc))
    app["reg"] = Registry()
    app.add_routes([web.get("/", index), web.get("/ws", viewer_ws), web.get("/agent", agent_ws)])
    app.on_startup.append(start_watchdog)
    app.on_cleanup.append(stop_watchdog)

    scheme = "https" if ssl_ctx else "http"
    log.info("listening on %s://%s:%d (token required; read relay.token locally if --token was omitted)",
             scheme, args.host, args.port)
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx, print=None, access_log=None)


if __name__ == "__main__":
    main()
