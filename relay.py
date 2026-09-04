#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webrc 中继：放在有公网 IP 的机器上，只做转发，不解码画面。

  被控端 agent.py ──(出站 WebSocket /agent)──▶ relay ◀──(WebSocket /ws)── 浏览器（家里电脑 / 手机）

用法：
  python relay.py --port 33445 --token <随机串> --cert relay.crt --key relay.key [--log relay.log]
  --token 省略时自动生成并写到同目录 relay.token，下次沿用；
  --cert/--key 省略则明文 http/ws（只适合内网联调）。
  浏览器打开 https://<公网IP>:33445/?token=<随机串>

协议（浏览器与 agent 之间的消息原样转发，relay 只认下面几种）：
  agent   → relay : 二进制帧（jpeg 条带 / h264 访问单元）→ 发给所有观众，不丢帧（增量帧丢了画面就错了）
  relay   → agent : {"t":"ack"}   一帧被所有观众看完后回执，agent 据此节流，慢链路上不会堆积
                    {"t":"kf"}    要一张关键帧（和键盘 {"t":"key"} 不是一回事）——只在缓存里没有可回放的关键帧时才发
                    {"t":"viewers","n":N}  在线观众数，0 时 agent 停止截屏
                    {"t":"release"}        有观众断开时让 agent 松开所有按住的键
  浏览器 → relay : {"t":"ack"}    看完一帧回执；{"t":"ping","ts":..} relay 直接回 pong；
                    {"t":"kf"}     解不出来/刚进来要关键帧：relay 有缓存就直接回放，没有才转给 agent；其他原样转给 agent
  观众超过 STALL_TIMEOUT 秒不回执就被踢掉重连（比如手机切后台），免得拖住别人。

关键帧缓存：relay 只看每帧头里的 flags（bit0 = 关键帧），记住最近一个关键帧起的所有帧。新观众进来直接把这串回放给他，
  agent 不用重新出关键帧（h264 下出关键帧要重开 ffmpeg，约 1 秒；而且会打断其他观众的画面）。
  缓存超过 CACHE_FRAMES 帧或 CACHE_BYTES 就作废，等 agent 下一个关键帧再开始记。
"""
import argparse
import asyncio
import json
import logging
import secrets
import ssl
import sys
import time
from collections import deque
from pathlib import Path

from aiohttp import web, WSMsgType

HERE = Path(__file__).resolve().parent
STALL_TIMEOUT = 6.0
CACHE_FRAMES = 200
CACHE_BYTES = 8 << 20
REPLAY_MIN_GAP = 2.0     # 同一观众两次回放至少隔这么久；更频繁的 kf 说明缓存本身解不了，转给 agent 要新的
log = logging.getLogger("relay")


class Viewer:
    __slots__ = ("ws", "peer", "unacked", "queue", "last_replay")

    def __init__(self, ws, peer):
        self.ws = ws
        self.peer = peer
        self.unacked = deque()   # (frame_id 或 None（回放的缓存帧）, sent_at)，按顺序回执
        self.queue = asyncio.Queue()   # 发给这个观众的消息队列，一个任务顺序发，保证帧序
        self.last_replay = 0.0


class Hub:
    def __init__(self):
        self.agent = None       # 被控端的 WebSocketResponse，同一时刻只认一个
        self.viewers = set()
        self.frames = {}        # frame_id -> 还没回执的观众集合
        self.next_id = 0
        self.cache = []         # 最近一个关键帧起的所有帧
        self.cache_bytes = 0
        self.n_frames = 0
        self.n_bytes = 0
        self.t_stat = time.monotonic()

    @staticmethod
    async def _send(ws, data):
        try:
            if isinstance(data, (bytes, bytearray)):
                await ws.send_bytes(data)
            else:
                await ws.send_str(data)
        except Exception:
            pass  # 单个连接坏了不影响别人，它自己的 handler 会收尾

    async def pump(self, v):
        """一个观众一个发送任务：队列里什么顺序进，就什么顺序发。"""
        while True:
            data = await v.queue.get()
            if data is None:
                return
            await self._send(v.ws, data)

    def broadcast(self, text):
        for v in self.viewers:
            v.queue.put_nowait(text)

    async def to_agent(self, text):
        if self.agent is not None:
            await self._send(self.agent, text)

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
        log.info("replay %d frames (%d KB) -> %s", len(self.cache), self.cache_bytes // 1024, v.peer)
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
        self.viewers.discard(v)
        for fid, _ in list(v.unacked):
            if fid is not None:
                self._settle(fid, v)
        v.unacked.clear()
        v.queue.put_nowait(None)


def check_token(request):
    if not secrets.compare_digest(request.query.get("token", ""), request.app["token"]):
        raise web.HTTPForbidden(text="bad token")


async def index(request):
    check_token(request)
    return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-store"})


async def viewer_ws(request):
    check_token(request)
    hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
    await ws.prepare(request)
    v = Viewer(ws, request.remote)
    hub.viewers.add(v)
    pump = asyncio.create_task(hub.pump(v))
    log.info("viewer + %s (now %d)", v.peer, len(hub.viewers))
    v.queue.put_nowait(json.dumps({"t": "agent", "online": hub.agent is not None}))
    await hub.viewers_changed()
    if not hub.replay(v):
        await hub.to_agent('{"t":"kf"}')
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                ev = json.loads(msg.data)
            except ValueError:
                continue
            t = ev.get("t")
            if t == "ack":
                hub.on_ack(v)
            elif t == "ping":
                v.queue.put_nowait(json.dumps({"t": "pong", "ts": ev.get("ts")}))
            elif t == "kf":
                if not hub.replay(v):
                    await hub.to_agent(msg.data)
            else:
                await hub.to_agent(msg.data)
    finally:
        hub.drop_viewer(v)
        pump.cancel()
        log.info("viewer - %s (now %d)", v.peer, len(hub.viewers))
        await hub.to_agent('{"t":"release"}')
        await hub.viewers_changed()
    return ws


async def agent_ws(request):
    check_token(request)
    hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 << 20)
    await ws.prepare(request)
    old, hub.agent = hub.agent, ws
    if old is not None:
        log.info("agent replaced, closing old one")
        asyncio.create_task(old.close(code=4000, message=b"replaced"))
    hub.frames.clear()
    hub.cache = []
    hub.cache_bytes = 0
    for v in hub.viewers:
        v.unacked.clear()
    log.info("agent + %s", request.remote)
    hub.broadcast(json.dumps({"t": "agent", "online": True}))
    await hub.viewers_changed()
    if hub.viewers:
        await hub.to_agent('{"t":"kf"}')
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                hub.on_frame(msg.data)
            elif msg.type == WSMsgType.TEXT:
                hub.broadcast(msg.data)
    finally:
        if hub.agent is ws:
            hub.agent = None
            hub.broadcast(json.dumps({"t": "agent", "online": False}))
        log.info("agent - %s", request.remote)
    return ws


async def watchdog(app):
    """踢掉长时间不回执的观众；每 10 秒记一行流量统计。"""
    hub = app["hub"]
    try:
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            for v in list(hub.viewers):
                if v.unacked and now - v.unacked[0][1] > STALL_TIMEOUT:
                    log.warning("viewer %s stalled (%d frames unacked), closing", v.peer, len(v.unacked))
                    hub.drop_viewer(v)
                    asyncio.create_task(v.ws.close(code=4001, message=b"stalled"))
            if now - hub.t_stat >= 10:
                dt = now - hub.t_stat
                if hub.n_frames:
                    log.info("%.1f fps  %.0f KB/s  viewers=%d  cache=%d frames/%d KB", hub.n_frames / dt, hub.n_bytes / 1024 / dt,
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


def load_token(given):
    if given:
        return given
    f = HERE / "relay.token"
    if f.exists():
        return f.read_text(encoding="utf-8").strip()
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
        handlers.append(logging.StreamHandler(sys.stdout))
    if args.log or sys.stdout is None:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(args.log or str(HERE / "relay.log"), maxBytes=2_000_000, backupCount=2, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%m-%d %H:%M:%S", handlers=handlers)

    ssl_ctx = None
    if args.cert:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(args.cert, args.key or args.cert)

    app = web.Application()
    app["token"] = load_token(args.token)
    app["hub"] = Hub()
    app.add_routes([web.get("/", index), web.get("/ws", viewer_ws), web.get("/agent", agent_ws)])
    app.on_startup.append(start_watchdog)
    app.on_cleanup.append(stop_watchdog)

    scheme = "https" if ssl_ctx else "http"
    log.info("listening on %s:%d  ->  %s://<this-host>:%d/?token=%s", args.host, args.port, scheme, args.port, app["token"])
    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_ctx, print=None, access_log=None)


if __name__ == "__main__":
    main()
