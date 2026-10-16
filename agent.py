#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""webrc 被控端：跑在要被远程控制的 Windows 电脑上。

截屏 → 缩放 → 编码 → WebSocket 送到 relay.py；从 relay 收鼠标键盘事件，注入本机。
只有出站连接：不监听端口、不需要公网 IP、不用开防火墙。

两种编码，浏览器页面上随时切：
  h264  默认。每帧 BGRA 喂给一个常驻的 ffmpeg 子进程（Intel 核显 h264_qsv / NVIDIA nvenc / AMD amf / CPU libx264，
        启动时自动探测），H.264 P 帧只编码变化的部分，码率封顶（默认 2 Mbps），看视频、拖窗口、滚网页都不会撑爆中继。
        浏览器用 WebCodecs 硬解。需要 PATH 里有 ffmpeg（winget install Gyan.FFmpeg），没有就自动退回 jpeg。
  jpeg  与上一帧按 64px 分块比对，只把变化的条带编成 JPEG。不依赖 ffmpeg，老浏览器也能看。

用法：
  python agent.py --relay wss://<公网IP>:33445 --token <串> --cafile relay.crt [--name 家里的电脑]
  python agent.py --relay ws://127.0.0.1:18765 --token <串> --dry-run    # 本机联调：只打印输入事件，不真的注入

选项：
  --name NAME     这台机器在页面「电脑」下拉框里的名字，默认本机 hostname。
                  一个中继可以同时挂好几台被控电脑，靠这个名字区分；同名的后来者会顶掉前一个
  --monitor N     一开始截哪个显示器：1（默认）、2/3…按系统枚举顺序，0=所有显示器拼成一张。页面工具栏上可随时切
  --codec auto|h264|jpeg   初始编码。auto（默认）= 先发 jpeg，页面要求 h264 时再切（新版页面一连上就会要求）；
                           h264 = 一开始就用 h264；jpeg = 不碰 ffmpeg
  --encoder auto|qsv|nvenc|amf|x264   h264 用哪个编码器，auto 按这个顺序探测，第一个能用的就用
  --ffmpeg PATH   ffmpeg 可执行文件，默认在 PATH 里找
  --capture auto|dxcam|mss   截屏方式。auto = 装了 dxcam 就用 DXGI 桌面复制（2560x1440 一帧 7 ms，画面没变时不截），
                           否则 mss（GDI，一帧 40 ms）。--monitor 0 只能 mss
  --width/--quality/--fps/--kbps   初始画质，浏览器页面上可随时改；width=0 表示不缩放；kbps 只对 h264 有效
  --log FILE      日志文件（用 pythonw 无窗口运行时必需，否则默认写到同目录 agent.log）

帧格式（二进制，小端），两种编码共用同一个头：
  头 <HHhhHH> = 整帧宽, 整帧高, 光标x, 光标y（帧内像素，-1 表示不在此屏）, flags, 条带数
      flags bit0 = 关键帧（jpeg：整帧；h264：IDR，前面带 SPS/PPS），bit1 = 数据是 H.264
  每条 <HHHHI> = x, y, 宽, 高, 字节数，后跟数据。h264 时恒为一条 (0,0,宽,高)，数据是 Annex B 的一个完整访问单元。
流控：每发一帧等中继回 {"t":"ack"}，最多 MAX_INFLIGHT 帧在途；没回执就不截屏，变化累积到下一帧里，
      这样慢链路上永远不会堆积几秒的旧画面。中继/浏览器要关键帧时发 {"t":"kf"}（键盘按键是 {"t":"key",...}，别搞混）；
      h264 下要关键帧 = 重开一个 ffmpeg（核显约 1 秒），所以中继自己缓存了最近一个关键帧起的所有帧，新观众进来直接回放，
      一般不会真的打到这里。
多显示器：页面发 {"t":"mon","i":N} 切到第 N 个显示器（编号同 --monitor，0 = 全部拼一张），切完下一帧是关键帧；
      这里每次有观众进来、以及每次切完，都把显示器列表广播给所有页面：
      {"t":"mons","cur":当前编号,"list":[{"i":编号,"x":左,"y":上,"w":宽,"h":高,"primary":是否主屏},...]}，
      页面据此画下拉框（只有一个显示器时不显示）。中继不认这两个消息，原样转发，所以中继不用升级。
锁屏：每秒查一次接收输入的桌面。锁屏 / UAC 安全桌面期间截屏线程卡住、一帧都出不来，所以另发
      {"t":"desk","locked":true/false}（状态变了广播一次，新观众进来时正锁着也补发一次），页面据此说明为什么没画面。
      同样由中继原样转发；老页面不认，照旧干等。
管理员窗口：Windows 的 UIPI 规定，前台窗口所属进程的完整性级别比注入者高（任务管理器、「以管理员身份运行」的程序、
      安装程序……），SendInput / SetCursorPos 一律静默丢弃——画面照常、键鼠全没反应，直到那个窗口失去前台。
      本进程不是管理员时，每个输入事件注入前先把光标「挪」到它现在的位置探一下（被拒返回 0、错误码 5，成功则什么都不发生）；
      被拒就查出前台是哪个窗口，发 {"t":"blocked","win":标题,"exe":进程名,"fixed":true/false/null} 给页面说明原因，
      并在观众点击时请 explorer 把所有窗口最小化（Win+M；普通进程对管理员窗口连 ShowWindow 都是拒绝访问，只有任务栏动得了它），
      fixed 表示这一招之后输入是不是又能进了（null = 只是移动光标，没有出手）。从管理员终端启动 agent 就完全没这个问题。
"""
import argparse
import asyncio
import ctypes
import json
import logging
import queue
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

# 必须在导入 mss/pynput 之前：让本进程按物理像素工作，高 DPI 缩放下截屏尺寸和鼠标坐标才对得上
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import aiohttp
import cv2
import mss
import numpy as np
from pynput.keyboard import Controller as Keyboard, Key
from pynput.mouse import Button, Controller as Mouse

try:
    import dxcam            # DXGI 桌面复制：2560x1440 一帧 7 ms，画面没变时直接告诉你「没新帧」
except ImportError:
    dxcam = None

MSS = getattr(mss, "MSS", None) or mss.mss  # mss 10 起改名 MSS，老版本回退到 mss.mss
log = logging.getLogger("agent")
user32 = ctypes.windll.user32

TILE = 64            # jpeg：分块比对的块大小
FULL_RATIO = 0.5     # jpeg：变化块超过这个比例就直接发整帧（条带太多不如整帧划算）
MAX_INFLIGHT = 2     # 最多几帧没收到中继回执就暂停截屏
ACK_TIMEOUT = 10.0   # 中继超过这么久不回执就当它回了（兼容旧中继、防死锁）；必须比中继踢掉卡住观众的 6 秒长
# h264：最多隔多少个「有变化的帧」插一个 IDR（画面不动时不喂帧，所以不是按时间算）。2560x1440 一个 IDR 150–250 KB，
# 是普通帧的 20–30 倍，中继出口几 Mbps 时每来一个画面就顿半秒；WebSocket 走 TCP 不丢包，用不着靠 IDR 纠错，
# 新观众要的关键帧另有 kf 请求 / 中继缓存，所以尽量少插（原来 120，30fps 下每 4 秒顿一下）
GOP = 1800
RECONNECT_MAX = 5    # 连不上中继时最多隔几秒重试：中继重启、断网恢复后几秒内就能重新上线（原来最多等 30 秒）
FLAG_KEY, FLAG_H264 = 1, 2


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def cursor_pos():
    pt = POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# 单独一份 user32，好给这几个函数设 64 位句柄的签名，不影响别处（pynput 也在用 user32）
_desk_u32 = ctypes.WinDLL("user32", use_last_error=True)
_desk_u32.OpenInputDesktop.restype = ctypes.c_void_p
_desk_u32.OpenInputDesktop.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
_desk_u32.GetUserObjectInformationW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                                                ctypes.POINTER(ctypes.c_uint)]
_desk_u32.CloseDesktop.argtypes = [ctypes.c_void_p]


def input_desktop():
    """现在接收键鼠输入的桌面叫什么：平时是 Default；锁屏、UAC 确认框、Ctrl+Alt+Del 界面是 Winlogon，
    普通进程打不开它，这时返回 None。截屏和注入都只对 Default 有效。"""
    h = _desk_u32.OpenInputDesktop(0, 0, 0x0001)   # DESKTOP_READOBJECTS
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(64)
        n = ctypes.c_uint()
        return buf.value if _desk_u32.GetUserObjectInformationW(h, 2, buf, ctypes.sizeof(buf), ctypes.byref(n)) else None  # UOI_NAME
    finally:
        _desk_u32.CloseDesktop(h)


_desk_u32.GetForegroundWindow.restype = ctypes.c_void_p
_desk_u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
_desk_u32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
_desk_u32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
_desk_u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
_k32.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.GetCurrentProcess.restype = ctypes.c_void_p
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_adv.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
_adv.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
_adv.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
_adv.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
_adv.GetSidSubAuthority.restype = ctypes.POINTER(ctypes.c_uint)
_adv.GetSidSubAuthority.argtypes = [ctypes.c_void_p, ctypes.c_uint]
IL_HIGH = 0x3000          # 管理员（UAC 提权后）；普通程序是 0x2000（medium），SYSTEM 是 0x4000


def process_il(hproc):
    """进程令牌的完整性级别（SECURITY_MANDATORY_*_RID），查不到返回 None。普通进程也能查管理员进程的（2026-09-26 实测）。"""
    tok = ctypes.c_void_p()
    if not _adv.OpenProcessToken(hproc, 0x0008, ctypes.byref(tok)):        # TOKEN_QUERY
        return None
    try:
        n = ctypes.c_uint()
        _adv.GetTokenInformation(tok, 25, None, 0, ctypes.byref(n))          # TokenIntegrityLevel
        buf = ctypes.create_string_buffer(n.value)
        if not _adv.GetTokenInformation(tok, 25, buf, n.value, ctypes.byref(n)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]           # TOKEN_MANDATORY_LABEL.Label.Sid
        return _adv.GetSidSubAuthority(sid, _adv.GetSidSubAuthorityCount(sid)[0] - 1)[0]
    finally:
        _k32.CloseHandle(tok)


def my_il():
    return process_il(_k32.GetCurrentProcess())


def il_name(il):
    return "?" if il is None else {0x1000: "low", 0x2000: "medium", 0x3000: "high (administrator)", 0x4000: "system"}.get(il & ~0xfff, hex(il))


def foreground_info():
    """前台窗口：标题、所属进程的 exe 名和完整性级别。没有前台窗口（正在切换 / 安全桌面）返回 None。"""
    hwnd = _desk_u32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_uint()
    _desk_u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    title = ctypes.create_unicode_buffer(256)
    _desk_u32.GetWindowTextW(hwnd, title, 256)
    exe, il = "?", None
    h = _k32.OpenProcess(0x1000, 0, pid.value)                                 # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.c_uint(1024)
            if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                exe = buf.value.rsplit("\\", 1)[-1]
            il = process_il(h)
        finally:
            _k32.CloseHandle(h)
    return {"hwnd": hwnd, "pid": pid.value, "title": title.value, "exe": exe, "il": il}


def inject_allowed():
    """现在往系统注入键鼠会不会被丢掉：把光标「挪」到它当前的位置——UIPI 拒绝时 SetCursorPos 返回 0（错误码 5），
    允许时什么都不发生。前台窗口所属进程的完整性级别比本进程高就会被拒；本进程是管理员时几乎不会。"""
    pt = POINT()
    if not _desk_u32.GetCursorPos(ctypes.byref(pt)):
        return True          # 查不到光标（桌面切走了）：另有 desk 消息说明，这里不算被拒
    return bool(_desk_u32.SetCursorPos(pt.x, pt.y))


def shell_minimize_all():
    """请 explorer 把所有窗口最小化（相当于 Win+M）：管理员窗口只有任务栏（explorer）动得了，普通权限的进程对它调
    ShowWindow / ShowWindowAsync / SetWindowPos 全是拒绝访问（2026-09-26 实测）。走 PowerShell 调 Shell.Application 的 COM，
    不用装 pywin32。explorer 收拾窗口是异步的，稍等一下再让调用方重新探测。"""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "(New-Object -ComObject Shell.Application).MinimizeAll()"],
                           capture_output=True, timeout=10, creationflags=0x08000000)   # CREATE_NO_WINDOW
        if r.returncode != 0:
            log.warning("MinimizeAll via explorer failed: %s", r.stderr.decode(errors="replace").strip()[:300])
            return False
        time.sleep(0.4)
        return True
    except Exception as e:
        log.warning("MinimizeAll via explorer failed: %s", e)
        return False


def list_monitors():
    """显示器列表（物理像素）：[0] 是所有显示器拼成的虚拟桌面，[1..] 是各个显示器，顺序按系统枚举。
    每次调用都重新枚举，插拔显示器后拿到的是新布局。"""
    with MSS() as sct:
        return [dict(m) for m in sct.monitors]


def _geom(m):
    return m["left"], m["top"], m["width"], m["height"]


def _is_primary(m):
    return bool(m.get("is_primary", _geom(m)[:2] == (0, 0)))   # 老版 mss 没有 is_primary：主屏原点必是 (0,0)


class Settings:
    def __init__(self, width, quality, fps, codec, kbps):
        self.width, self.quality, self.fps, self.codec, self.kbps = width, quality, fps, codec, kbps

    def update(self, ev):
        self.width = max(0, int(ev.get("width", self.width)))
        self.quality = max(10, min(95, int(ev.get("quality", self.quality))))
        self.fps = max(1, min(30, int(ev.get("fps", self.fps))))
        if ev.get("codec") in ("h264", "jpeg"):
            self.codec = ev["codec"]
        self.kbps = max(300, min(20000, int(ev.get("kbps", self.kbps))))

    def snapshot(self):
        return (self.width, self.quality, self.fps, self.codec, self.kbps)


# ---------------------------------------------------------------- H.264 via ffmpeg ----------
# 每种编码器：ffmpeg 里的名字、放在 -i 前面的全局参数（硬件设备）、按码率生成的编码参数。
# 全部按「零延迟」配：无 B 帧、无 lookahead、VBR 封顶。2026-09-03 在 i7-14700 + UHD 770 上实测
# qsv 1080p 每帧 9 ms、1440p 15 ms；x264 差不多但占 CPU。nvenc/amf 本机没卡，参数按官方文档写，未实测。
def _vbr(kbps):
    return ["-b:v", f"{int(kbps * 0.8)}k", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps}k"]


# -pix_fmt 必须显式给 4:2:0：输入是 BGRA，不给的话 ffmpeg 会给 libx264 自动选 4:4:4（profile 244），
# 浏览器的 H.264 解码器只认 4:2:0，遇到这种流不报错、只是永远不出画面。
ENCODERS = {
    # 指定用 Intel 的 D3D11 设备建 MFX 会话：不指定时 ffmpeg 自己找，本机多了两块虚拟显卡（ToDesk/GameViewer），要多花 0.7 秒
    "qsv": ("h264_qsv", ["-init_hw_device", "d3d11va=dx:,vendor_id=0x8086", "-init_hw_device", "qsv=hw@dx", "-filter_hw_device", "hw"],
            lambda k: ["-pix_fmt", "nv12", "-preset", "veryfast", "-async_depth", "1", "-look_ahead", "0",
                       "-scenario", "displayremoting", "-forced_idr", "1"] + _vbr(k)),
    "nvenc": ("h264_nvenc", [],
              lambda k: ["-pix_fmt", "nv12", "-preset", "p3", "-tune", "ll", "-rc", "vbr", "-delay", "0", "-zerolatency", "1",
                         "-forced-idr", "1"] + _vbr(k)),
    "amf": ("h264_amf", [],
            lambda k: ["-pix_fmt", "nv12", "-usage", "lowlatency", "-rc", "vbr_latency"] + _vbr(k)),
    "x264": ("libx264", [],
             lambda k: ["-pix_fmt", "yuv420p", "-profile:v", "high", "-preset", "veryfast", "-tune", "zerolatency",
                        "-threads", "2", "-crf", "24"] + _vbr(k)[2:]),
}
ENCODER_ORDER = ["qsv", "nvenc", "amf", "x264"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def encoder_cmd(ffmpeg, name, w, h, fps, kbps):
    codec, hwdev, opts = ENCODERS[name]
    return [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats", *hwdev,
            # 别加 -fflags nobuffer：它会把探测阶段读到的第一帧直接丢掉，表现为「永远慢一帧」
            "-f", "rawvideo", "-pix_fmt", "bgra", "-video_size", f"{w}x{h}", "-framerate", str(fps), "-i", "pipe:0",
            "-an", "-c:v", codec, *opts(kbps), "-g", str(GOP), "-bf", "0",
            "-bsf:v", "dump_extra",            # 每个 IDR 前都带 SPS/PPS，观众中途进来也能解
            "-fps_mode", "passthrough", "-flush_packets", "1",
            # 用 AVI 封装而不是裸 h264：AVI 里每帧是一个带长度的 00dc 块，一到就能切出来；
            # 裸流要等下一帧的起始码才知道上一帧结束，画面最后一帧永远压在编码器里出不来
            "-f", "avi", "pipe:1"]


def probe_encoder(ffmpeg, name):
    """用 lavfi 造两帧编一下，能跑通就认为这个编码器可用。"""
    codec, hwdev, opts = ENCODERS[name]
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", *hwdev, "-f", "lavfi", "-i", "color=c=black:s=320x240:r=12",
           "-frames:v", "2", "-an", "-c:v", codec, *opts(1000), "-g", str(GOP), "-bf", "0", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    err = r.stderr.decode(errors="replace").strip().splitlines()
    return r.returncode == 0, (err[-1] if err else "")


def pick_encoder(ffmpeg, wanted):
    names = ENCODER_ORDER if wanted == "auto" else [wanted]
    for name in names:
        t0 = time.monotonic()
        ok, msg = probe_encoder(ffmpeg, name)
        log.info("probe %s (%s): %s  %.1fs %s", name, ENCODERS[name][0], "ok" if ok else "unavailable", time.monotonic() - t0, msg if not ok else "")
        if ok:
            return name
    return None


class AviChunks:
    """增量解析 ffmpeg 吐出来的 AVI 流：feed() 进字节，吐出每个视频块（'00dc'/'00db'）的内容，一块正好一帧。"""

    def __init__(self):
        self.buf = bytearray()
        self.pos = 0

    def feed(self, data):
        self.buf += data
        out = []
        while len(self.buf) - self.pos >= 8:
            tag = bytes(self.buf[self.pos:self.pos + 4])
            size = int.from_bytes(self.buf[self.pos + 4:self.pos + 8], "little")
            if tag in (b"RIFF", b"LIST"):
                self.pos += 12          # 进到列表里面去，不整体跳过
                continue
            end = self.pos + 8 + size + (size & 1)
            if len(self.buf) < end:
                break
            if tag[2:] in (b"dc", b"db"):
                out.append(bytes(self.buf[self.pos + 8:self.pos + 8 + size]))
            self.pos = end
        if self.pos > 1 << 20:
            del self.buf[:self.pos]
            self.pos = 0
        return out


def has_idr(au):
    """Annex B 访问单元里有没有 IDR 片（NAL 类型 5）。"""
    i = 0
    while True:
        j = au.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= len(au):
            return False
        t = au[j + 3] & 0x1F
        if t == 5:
            return True
        if t == 1:
            return False
        i = j + 3


class EncoderError(Exception):
    pass


class H264Encoder:
    """一个常驻的 ffmpeg 子进程：stdin 喂 BGRA 原始帧，stdout 吐 AVI 封装的 H.264。只在截屏线程里用。"""

    def __init__(self, ffmpeg, name, w, h, fps, kbps):
        self.params = (name, w, h, fps, kbps)
        self.frames = 0
        self._q = queue.Queue()
        self._err = deque(maxlen=12)
        cmd = encoder_cmd(ffmpeg, name, w, h, fps, kbps)
        log.info("ffmpeg start: %s %dx%d %dfps %dkbps", ENCODERS[name][0], w, h, fps, kbps)
        log.debug("ffmpeg cmd: %s", " ".join(cmd))
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  creationflags=NO_WINDOW)
        threading.Thread(target=self._reader, daemon=True, name="ffmpeg-out").start()
        threading.Thread(target=self._stderr, daemon=True, name="ffmpeg-err").start()

    def _reader(self):
        parser = AviChunks()
        try:
            while True:
                chunk = self.p.stdout.read1(1 << 16)
                if not chunk:
                    break
                for au in parser.feed(chunk):
                    self._q.put(au)
        except (OSError, ValueError):
            pass
        self._q.put(None)   # 进程没了

    def _stderr(self):
        try:
            for line in self.p.stderr:
                s = line.decode(errors="replace").rstrip()
                if s:
                    self._err.append(s)
                    log.info("ffmpeg: %s", s)
        except (OSError, ValueError):
            pass

    def encode(self, img):
        """喂一帧（C 连续的 HxWx4 uint8 数组），返回编好的访问单元。首帧要等编码器初始化（核显约 1 秒）。"""
        try:
            self.p.stdin.write(img)
            self.p.stdin.flush()
        except (OSError, ValueError) as e:
            raise EncoderError(f"write failed: {e}; {' | '.join(self._err)}")
        try:
            au = self._q.get(timeout=15.0 if self.frames == 0 else 3.0)
        except queue.Empty:
            raise EncoderError(f"no output from ffmpeg; {' | '.join(self._err)}")
        if au is None:
            raise EncoderError(f"ffmpeg exited ({self.p.poll()}); {' | '.join(self._err)}")
        self.frames += 1
        return au

    def close(self):
        for f in (self.p.stdin, self.p.stdout, self.p.stderr):
            try:
                f.close()
            except OSError:
                pass
        try:
            self.p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.p.kill()


# ---------------------------------------------------------------- 截屏 ----------
class Capture:
    """截屏后端。优先 DXGI 桌面复制（dxcam）：快，而且画面没变时不用截、不用比对；
    装不上 / 建不出来 / 中途出错就退回 mss（GDI BitBlt，2560x1440 一帧 40 ms），之后每 30 秒再试一次 dxcam。
    全部只在截屏线程里调用。锁屏和 UAC 安全桌面期间 dxcam 会在内部一直重试到解锁为止（那时 GDI 也只能截到黑屏）。"""

    RETRY = 30.0

    def __init__(self, monitor, monitor_index, wanted):
        self.mon = monitor
        self.index = monitor_index
        self.wanted = "mss" if monitor_index == 0 else wanted   # auto / dxcam / mss；整个虚拟桌面只有 mss 能截，别每 30 秒试一次 dxcam
        self.cam = None
        self.sct = None
        self.last = None               # dxcam 给的最近一帧（BGRA 原始尺寸），要强制发帧而画面没变时拿它再编一次
        self.retry_at = 0.0
        self.backend = None

    def _find_output(self):
        """按屏幕坐标把 mss 的显示器对到 dxcam 的 (device, output)。"""
        factory = getattr(dxcam, "__factory")
        m = self.mon
        for di, outputs in enumerate(factory.outputs):
            for oi, out in enumerate(outputs):
                r = out.desc.DesktopCoordinates
                if (r.left, r.top, r.right - r.left, r.bottom - r.top) == (m["left"], m["top"], m["width"], m["height"]):
                    return di, oi
        return None

    def _open_dxcam(self):
        if dxcam is None:
            raise RuntimeError("dxcam not installed")
        if self.index == 0:
            raise RuntimeError("--monitor 0（整个虚拟桌面）dxcam 不支持")
        where = self._find_output()
        if where is None:
            raise RuntimeError(f"no DXGI output matches monitor {self.mon}")
        cam = dxcam.create(device_idx=where[0], output_idx=where[1], output_color="BGRA")
        if (cam.width, cam.height) != (self.mon["width"], self.mon["height"]):
            cam.release()
            raise RuntimeError(f"dxcam size {cam.width}x{cam.height} != monitor {self.mon['width']}x{self.mon['height']}")
        self.cam = cam
        self.last = None
        self.backend = f"dxcam(device {where[0]} output {where[1]})"
        log.info("capture: %s %dx%d", self.backend, cam.width, cam.height)

    def _open(self):
        if self.wanted != "mss" and self.cam is None and time.monotonic() >= self.retry_at:
            try:
                self._open_dxcam()
            except Exception as e:
                self.retry_at = time.monotonic() + self.RETRY
                if self.wanted == "dxcam":
                    raise
                log.warning("dxcam unavailable, using mss: %s", e)
        if self.sct is None:
            self.sct = MSS()
        if self.cam is None and self.backend != "mss":
            self.backend = "mss"
            log.info("capture: mss (GDI) %dx%d", self.mon["width"], self.mon["height"])

    def _drop_dxcam(self, why):
        log.warning("dxcam failed, back to mss for %.0fs: %s", self.RETRY, why)
        try:
            self.cam.release()
        except Exception:
            pass
        self.cam = None
        self.last = None
        self.backend = None
        self.retry_at = time.monotonic() + self.RETRY

    def grab(self, need):
        """返回 (BGRA 图, 是否新画面)；画面没变而且 need=False 时返回 None，什么都不用做。"""
        self._open()
        if self.cam is not None:
            try:
                f = self.cam.grab()
                if f is None and need and self.last is None:
                    for _ in range(30):          # 刚建好的复制器要等一小会儿才有第一帧
                        time.sleep(0.01)
                        f = self.cam.grab()
                        if f is not None:
                            break
            except Exception as e:
                self._drop_dxcam(e)
            else:
                if f is not None:
                    self.last = f
                    return f, True
                if not need:
                    return None
                if self.last is not None:
                    return self.last, False
                self._drop_dxcam("no first frame")
        shot = self.sct.grab(self.mon)
        return np.frombuffer(shot.raw, np.uint8).reshape(shot.height, shot.width, 4), True

    def close(self):
        if self.cam is not None:
            try:
                self.cam.release()
            except Exception:
                pass
            self.cam = None
        if self.sct is not None:
            self.sct.close()
            self.sct = None


class Grabber:
    """截屏 + 缩放 + 变化检测 + 编码。截屏对象不能跨线程用，所以固定在一个工作线程里跑。"""

    def __init__(self, monitor_index, ffmpeg, encoder, capture):
        self.pool = ThreadPoolExecutor(max_workers=1)
        self._prev = None      # 上一次「发出去」的帧（缩放后），没发出去的变化会累积到下一帧
        self.force = True      # 下一帧发关键帧（新观众 / 改画质后）；h264 下意味着重开编码器
        self.ffmpeg = ffmpeg
        self.encoder = encoder         # ENCODERS 里的名字；None = 没有可用的 h264 编码器
        self.enc = None                # 当前的 H264Encoder
        self.enc_failures = deque(maxlen=3)
        self.capture_mode = capture    # auto / dxcam / mss，换显示器时按它重建截屏后端
        self.monitors = list_monitors()
        if monitor_index >= len(self.monitors):
            raise SystemExit(f"--monitor {monitor_index} 不存在，本机有 {len(self.monitors) - 1} 个显示器")
        self.index = monitor_index
        self.monitor = self.monitors[monitor_index]   # 主线程和事件循环只读它；换显示器时整个替换（不原地改），读到的永远是完整的一份
        log.info("monitor %d: %s", monitor_index, self.monitor)
        self.cap = Capture(self.monitor, monitor_index, capture)

    def _switch(self, index):
        """在截屏线程里换显示器：重建截屏后端，下一帧强制关键帧（h264 下会重开编码器）。"""
        self.monitors = list_monitors()
        if not 0 <= index < len(self.monitors):
            raise ValueError(f"显示器 {index} 不存在，本机有 {len(self.monitors) - 1} 个")
        self.cap.close()
        self.index = index
        self.monitor = self.monitors[index]
        self.cap = Capture(self.monitor, index, self.capture_mode)
        self._prev = None
        self.force = True
        log.info("switched to monitor %d: %s", index, self.monitor)

    def _refresh(self):
        """重新枚举显示器（有观众进来时调用）。插拔过显示器、当前这块的位置尺寸变了或没了，就在原编号（没了就 1）上重建。"""
        self.monitors = list_monitors()
        index = self.index if self.index < len(self.monitors) else 1
        if index != self.index or _geom(self.monitors[index]) != _geom(self.monitor):
            log.info("monitor layout changed, reopening monitor %d", index)
            self._switch(index)

    async def switch(self, index):
        await asyncio.get_running_loop().run_in_executor(self.pool, self._switch, index)

    async def refresh_monitors(self):
        await asyncio.get_running_loop().run_in_executor(self.pool, self._refresh)

    @staticmethod
    def _strips(mask, w, h):
        """把变化块的掩码（行×列）合并成每行的连续条带 [(x, y, w, h), ...]"""
        out = []
        rows, cols = mask.shape
        for r in range(rows):
            row = mask[r]
            c = 0
            while c < cols:
                if not row[c]:
                    c += 1
                    continue
                c0 = c
                while c < cols and row[c]:
                    c += 1
                x0, y0 = c0 * TILE, r * TILE
                out.append((x0, y0, min((c - c0) * TILE, w - x0), min(TILE, h - y0)))
        return out

    def _size(self, width):
        """缩放到 width 宽（0 = 不缩放）后的帧尺寸，不用截屏就能算出来。"""
        mw, mh = self.monitor["width"], self.monitor["height"]
        if width and mw > width:
            return width, mh * width // mw
        return mw, mh

    def _cursor(self, w, h):
        mon = self.monitor
        x, y = cursor_pos()
        cx = (x - mon["left"]) * w // mon["width"]
        cy = (y - mon["top"]) * h // mon["height"]
        if not (0 <= cx < w and 0 <= cy < h):
            cx = cy = -1
        return cx, cy

    def _capture(self, width, need):
        """截屏并缩放。画面没变且不强制时返回 None；否则返回 (BGRA 图, 是否新画面)。"""
        res = self.cap.grab(need)
        if res is None:
            return None
        img, fresh = res
        w, h = self._size(width)
        if (w, h) != (img.shape[1], img.shape[0]):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        return img, fresh

    def _grab(self, settings):
        width, quality, fps, codec, kbps = settings
        if codec == "h264" and self.encoder and self.h264_alive():
            return self._grab_h264(width, fps, kbps)
        if self.enc is not None:       # 切回 jpeg 了，把 ffmpeg 收掉
            self._close_encoder()
        return self._grab_jpeg(width, quality)

    def h264_alive(self):
        """h264 连续失败 3 次（60 秒内）就放弃，改用 jpeg，免得反复重开 ffmpeg。"""
        f = self.enc_failures
        return not (len(f) == f.maxlen and time.monotonic() - f[0] < 60)

    def _close_encoder(self):
        if self.enc is not None:
            self.enc.close()
            self.enc = None
        self._prev = None

    def _grab_h264(self, width, fps, kbps):
        w, h = self._size(width)
        w, h = w & ~1, h & ~1                                # H.264 4:2:0 要求偶数尺寸
        cx, cy = self._cursor(w, h)
        params = (self.encoder, w, h, fps, kbps)
        restart = self.enc is not None and (self.enc.params != params or (self.force and self.enc.frames > 0))
        need = self.force or restart or self.enc is None or self._prev is None
        res = self._capture(width, need)
        if res is None:                                      # 画面没变
            return None, w, h, cx, cy, False, 0
        img, _ = res
        if (h, w) != img.shape[:2]:
            img = img[:h, :w]
        img = np.ascontiguousarray(img)
        prev = self._prev
        if restart:
            self._close_encoder()
            prev = None
        if not (need or prev is None or prev.shape != img.shape) and np.array_equal(img, prev):
            return None, w, h, cx, cy, False, 0
        try:
            if self.enc is None:
                self.enc = H264Encoder(self.ffmpeg, *params)
            au = self.enc.encode(img)
        except EncoderError as e:
            self.enc_failures.append(time.monotonic())
            log.warning("h264 encoder failed (%d/3): %s", len(self.enc_failures), e)
            self._close_encoder()
            if not self.h264_alive():
                log.warning("h264 given up, falling back to jpeg")
            return None, w, h, cx, cy, False, 0
        key = has_idr(au)
        flags = FLAG_H264 | (FLAG_KEY if key else 0)
        payload = b"".join((struct.pack("<HHhhHH", w, h, cx, cy, flags, 1), struct.pack("<HHHHI", 0, 0, w, h, len(au)), au))
        self._prev = img
        self.force = False
        return payload, w, h, cx, cy, key, 1

    def _grab_jpeg(self, width, quality):
        w, h = self._size(width)
        cx, cy = self._cursor(w, h)
        prev = self._prev
        res = self._capture(width, self.force or prev is None or prev.shape[:2] != (h, w))
        if res is None:                                      # 画面没变
            return None, w, h, cx, cy, False, 0
        img = cv2.cvtColor(res[0], cv2.COLOR_BGRA2BGR)
        key = self.force or prev is None or prev.shape != img.shape
        strips = None
        if not key:
            diff = (img != prev).any(axis=2)
            if not diff.any():
                return None, w, h, cx, cy, False, 0
            rows, cols = -(-h // TILE), -(-w // TILE)
            pad = np.zeros((rows * TILE, cols * TILE), dtype=bool)
            pad[:h, :w] = diff
            mask = pad.reshape(rows, TILE, cols, TILE).any(axis=(1, 3))
            if mask.mean() > FULL_RATIO:
                key = True
            else:
                strips = self._strips(mask, w, h)
        if key:
            strips = [(0, 0, w, h)]

        params = [cv2.IMWRITE_JPEG_QUALITY, quality]
        if quality >= 70 and hasattr(cv2, "IMWRITE_JPEG_SAMPLING_FACTOR"):
            params += [cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444]  # 高画质时不压色度，文字更清楚
        parts = [struct.pack("<HHhhHH", w, h, cx, cy, FLAG_KEY if key else 0, len(strips))]
        for sx, sy, sw, sh in strips:
            ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(img[sy:sy + sh, sx:sx + sw]), params)
            b = buf.tobytes() if ok else b""
            parts.append(struct.pack("<HHHHI", sx, sy, sw, sh, len(b)))
            parts.append(b)
        self._prev = img
        self.force = False
        return b"".join(parts), w, h, cx, cy, key, len(strips)

    async def grab(self, settings):
        return await asyncio.get_running_loop().run_in_executor(self.pool, self._grab, settings)

    def _close_all(self):
        self._close_encoder()
        self.cap.close()

    async def close(self):
        await asyncio.get_running_loop().run_in_executor(self.pool, self._close_all)


# ---------------------------------------------------------------- 输入注入 ----------
SPECIAL = {
    "Enter": Key.enter, "Backspace": Key.backspace, "Tab": Key.tab, "Escape": Key.esc,
    "Delete": Key.delete, "Insert": Key.insert, "Home": Key.home, "End": Key.end,
    "PageUp": Key.page_up, "PageDown": Key.page_down,
    "ArrowUp": Key.up, "ArrowDown": Key.down, "ArrowLeft": Key.left, "ArrowRight": Key.right,
    "Shift": Key.shift, "Control": Key.ctrl, "Alt": Key.alt, "Meta": Key.cmd, "OS": Key.cmd,
    "CapsLock": Key.caps_lock, "NumLock": Key.num_lock, "ScrollLock": Key.scroll_lock,
    "ContextMenu": Key.menu, "PrintScreen": Key.print_screen, "Pause": Key.pause, " ": Key.space,
}
SPECIAL.update({f"F{i}": getattr(Key, f"f{i}") for i in range(1, 13)})
BUTTONS = {0: Button.left, 1: Button.middle, 2: Button.right}


class Input:
    """把浏览器发来的事件注入本机。坐标是 0..1 的比例，换算到当前所截显示器的物理像素（切了屏就跟着切）。"""

    INJECTED = ("move", "down", "up", "click", "scroll", "key", "text")   # 会真往系统里注入的事件（release 只松自己按过的）

    def __init__(self, grabber, dry_run):
        self.grabber = grabber
        self.dry = dry_run
        self.mouse = Mouse()
        self.kb = Keyboard()
        self.held_keys = set()
        self.held_buttons = set()
        il = my_il()
        self.elevated = il is not None and il >= IL_HIGH   # 管理员进程注入不会被 UIPI 拒，省掉每个事件前的探测
        self.blocked_hwnd = None       # 上次挡住注入的前台窗口，同一个窗口不反复记日志
        self.blocked_at = 0.0
        self.last_report = 0.0         # 上次告诉页面「被挡住了」的时刻（鼠标移动 30 次/秒，不能每次都发）
        self.last_rescue = 0.0         # 上次请 explorer 最小化窗口的时刻

    def _pos(self, ev):
        mon = self.grabber.monitor
        nx = min(max(float(ev["x"]), 0.0), 1.0)
        ny = min(max(float(ev["y"]), 0.0), 1.0)
        return (mon["left"] + int(nx * (mon["width"] - 1)),
                mon["top"] + int(ny * (mon["height"] - 1)))

    @staticmethod
    def _key(k):
        if k in SPECIAL:
            return SPECIAL[k]
        if len(k) == 1:
            return k
        return None

    def handle(self, ev):
        """注入一个事件。正常返回 None；注入会被系统丢掉（前台是权限更高的窗口）时丢弃这个事件，
        返回一个 dict 让 Agent 去告诉页面、必要时自救——就算马上救回来，这一下点的也已经不是用户看到的那个位置了。"""
        t = ev.get("t")
        if self.dry:
            log.info("dry-run %s", json.dumps(ev, ensure_ascii=False))
            return None
        if t in self.INJECTED and not self.elevated and not inject_allowed():
            return self._blocked(t)
        try:
            if t == "move":
                self.mouse.position = self._pos(ev)
            elif t in ("down", "up"):
                if "x" in ev:
                    self.mouse.position = self._pos(ev)
                btn = BUTTONS.get(int(ev.get("b", 0)), Button.left)
                if t == "down":
                    self.mouse.press(btn)
                    self.held_buttons.add(btn)
                else:
                    self.mouse.release(btn)
                    self.held_buttons.discard(btn)
            elif t == "click":
                if "x" in ev:
                    self.mouse.position = self._pos(ev)
                self.mouse.click(BUTTONS.get(int(ev.get("b", 0)), Button.left), int(ev.get("n", 1)))
            elif t == "scroll":
                self.mouse.scroll(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
            elif t == "key":
                key = self._key(str(ev.get("k", "")))
                if key is None:
                    log.debug("unknown key %r", ev.get("k"))
                elif ev.get("d"):
                    self.kb.press(key)
                    self.held_keys.add(key)
                else:
                    self.kb.release(key)
                    self.held_keys.discard(key)
            elif t == "text":
                self.kb.type(str(ev.get("s", "")))
            elif t == "release":
                self.release_all()
        except Exception as e:  # pynput 对个别按键可能抛异常，别让它把连接搞断
            log.warning("input %s failed: %s", t, e)
        return None

    def _blocked(self, t):
        """注入被拒：查前台是谁，换了窗口就记一次日志；返回给页面的说明（限频），点击时顺带请求自救。"""
        if (input_desktop() or "").lower() != "default":
            return None                                   # 锁屏 / UAC 安全桌面：desk 消息已经在说明了，不重复
        now = time.monotonic()
        fg = foreground_info() or {}
        if fg.get("hwnd") != self.blocked_hwnd or now - self.blocked_at > 60:
            self.blocked_hwnd, self.blocked_at = fg.get("hwnd"), now
            log.warning("input dropped by Windows (UIPI): foreground window %r is %s at %s, this agent is %s "
                        "-- start agent.py from an administrator terminal to control such windows",
                        fg.get("title", "?"), fg.get("exe", "?"), il_name(fg.get("il")), il_name(my_il()))
        rescue = t in ("down", "click") and now - self.last_rescue > 5
        if rescue:
            self.last_rescue = now
        elif now - self.last_report < 2:
            return None
        self.last_report = now
        return {"win": fg.get("title", ""), "exe": fg.get("exe", "?"), "rescue": rescue}

    def release_all(self):
        for k in list(self.held_keys):
            try:
                self.kb.release(k)
            except Exception:
                pass
        for b in list(self.held_buttons):
            try:
                self.mouse.release(b)
            except Exception:
                pass
        self.held_keys.clear()
        self.held_buttons.clear()


# ---------------------------------------------------------------- 主循环 ----------
class Agent:
    def __init__(self, args, encoder):
        self.args = args
        # auto = 先按 jpeg 发，页面的 config 里要求 h264 才切：旧版 index.html 不认 h264 帧，这样配新旧哪个中继都能用
        codec = "h264" if args.codec == "h264" and encoder else "jpeg"
        self.settings = Settings(args.width, args.quality, args.fps, codec, args.kbps)
        self.grabber = Grabber(args.monitor, args.ffmpeg, encoder, args.capture)
        self.input = Input(self.grabber, args.dry_run)
        self.viewers = 0
        self.inflight = deque()   # 已发出、中继还没回执的帧的发送时刻
        self._ack_warned = False
        self.last_input = 0.0     # 最近一次收到观众键鼠输入的时刻：刚操作过就别放慢截屏
        self.kick = None          # asyncio.Event，有输入时叫醒慢速等待中的截屏循环（在 run() 里建）
        self.locked = None        # 输入桌面是不是 Default 以外（锁屏 / UAC）；None = 这次连接还没查过

    def _ssl(self):
        if not self.args.relay.lower().startswith("wss"):
            return True
        if self.args.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.args.cafile:
            return ssl.create_default_context(cafile=self.args.cafile)
        return True  # 系统 CA；自签证书要么给 --cafile，要么 --insecure

    async def run(self):
        url = (self.args.relay.rstrip("/") + "/agent?token=" + quote(self.args.token, safe="")
               + "&name=" + quote(self.args.name, safe=""))
        ssl_ctx = self._ssl()
        self.kick = asyncio.Event()
        backoff = 1
        fails = 0
        try:
            while True:
                try:
                    async with aiohttp.ClientSession() as sess:
                        async with sess.ws_connect(url, ssl=ssl_ctx, heartbeat=20, max_msg_size=1 << 20) as ws:
                            log.info("connected: %s as [%s]%s", self.args.relay, self.args.name,
                                     f" after {fails} failed attempts" if fails > 3 else "")
                            backoff = 1
                            fails = 0
                            self.inflight.clear()
                            self._ack_warned = False
                            self.grabber.force = True
                            self.locked = None
                            await self._session(ws)
                    log.warning("relay closed the connection")
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                    fails += 1
                    # 中继长时间连不上时每 5 秒失败一次，别刷满日志：前 3 次照记，之后每 60 次（约 5 分钟）记一次
                    if fails <= 3 or fails % 60 == 0:
                        log.warning("connect failed (%d): %s", fails, e)
                except Exception:
                    log.exception("session error")
                finally:
                    self.viewers = 0
                    self.input.release_all()
                if fails <= 3:
                    log.info("retry in %ds", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_MAX)
        finally:
            await self.grabber.close()

    async def _session(self, ws):
        tasks = {asyncio.create_task(self._reader(ws)), asyncio.create_task(self._sender(ws)),
                 asyncio.create_task(self._desk_watch(ws))}
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
            try:
                await t
            except BaseException:
                pass
        for t in done:
            t.result()

    async def _reader(self, ws):
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                ev = json.loads(msg.data)
            except ValueError:
                continue
            t = ev.get("t")
            if t == "ack":
                if self.inflight:
                    self.inflight.popleft()
            elif t == "kf":
                self.grabber.force = True
            elif t == "viewers":
                # 新观众要的关键帧由中继回放缓存解决，中继缓存里没有时它会另发 kf 过来，这里不用强制
                self.viewers = int(ev.get("n", 0))
                log.info("viewers: %d", self.viewers)
                if self.viewers == 0:
                    self.input.release_all()
                else:
                    asyncio.create_task(self._send_monitors(ws, refresh=True))   # 新观众要显示器列表画下拉框
                    if self.locked:
                        asyncio.create_task(self._send_desk(ws))                  # 进来时正锁着屏，告诉他为什么没画面
            elif t == "mon":
                asyncio.create_task(self._switch_monitor(ws, ev))
            elif t == "config":
                s = self.settings
                before = s.snapshot()
                s.update(ev)
                if s.snapshot() != before:      # 每个观众连上都会发一遍 config，没变就别重开编码器
                    self.grabber.force = True
                    log.info("config: codec=%s width=%d quality=%d fps=%d kbps=%d", s.codec, s.width, s.quality, s.fps, s.kbps)
            else:
                blocked = self.input.handle(ev)
                if blocked:
                    asyncio.create_task(self._report_blocked(ws, blocked))
                self.last_input = time.monotonic()
                self.kick.set()

    async def _report_blocked(self, ws, info):
        """注入被 UIPI 拒了：要自救就先请 explorer 把窗口都最小化（PowerShell 起一下要几百毫秒，放线程里跑），
        再探一次告诉页面成没成；只是移动光标就直接说明原因。"""
        fixed = None
        if info.pop("rescue"):
            ok = await asyncio.get_running_loop().run_in_executor(None, shell_minimize_all)
            fixed = bool(ok and inject_allowed())
            log.info("rescue: MinimizeAll via explorer %s, input %s", "done" if ok else "failed",
                     "allowed again" if fixed else "still blocked")
        try:
            await ws.send_str(json.dumps({"t": "blocked", **info, "fixed": fixed}, ensure_ascii=False))
        except Exception as e:
            log.debug("send blocked failed: %s", e)

    async def _desk_watch(self, ws):
        """每秒看一眼输入桌面。锁屏 / UAC 期间截屏线程卡在 dxcam 的内部重试里，一帧都出不来，页面只能干等
        「等待画面…」，看的人不知道为什么（日志里有过一分钟连刷 5 次页面的）——所以单独告诉页面一声。"""
        while True:
            try:
                locked = (input_desktop() or "").lower() != "default"
            except Exception as e:      # 查不了就当没锁，别因为这个把连接搞断
                log.debug("input desktop check failed: %s", e)
                locked = False
            if locked != self.locked:
                if self.locked is not None or locked:
                    log.info("input desktop: %s", "locked / secure desktop" if locked else "Default")
                self.locked = locked
                await self._send_desk(ws)
            await asyncio.sleep(1)

    async def _send_desk(self, ws):
        try:
            await ws.send_str(json.dumps({"t": "desk", "locked": bool(self.locked)}))
        except Exception as e:
            log.debug("send desk failed: %s", e)

    def _monitors_msg(self):
        g = self.grabber
        mons = [{"i": i, "x": m["left"], "y": m["top"], "w": m["width"], "h": m["height"], "primary": i > 0 and _is_primary(m)}
                for i, m in enumerate(g.monitors)]
        return json.dumps({"t": "mons", "cur": g.index, "list": mons})

    async def _send_monitors(self, ws, refresh):
        """把显示器列表广播给所有页面。截屏线程忙时（比如正在切屏）这里会等它。"""
        try:
            if refresh:
                await self.grabber.refresh_monitors()
            await ws.send_str(self._monitors_msg())
        except Exception as e:      # 连接刚好断了之类的，主循环会重连，这里别当成任务异常刷日志
            log.debug("send monitors failed: %s", e)

    async def _switch_monitor(self, ws, ev):
        try:
            index = int(ev.get("i", 1))
        except (TypeError, ValueError):
            return
        if index == self.grabber.index:
            await self._send_monitors(ws, refresh=False)
            return
        try:
            await self.grabber.switch(index)
        except ValueError as e:
            log.warning("switch monitor: %s", e)
        except Exception:
            log.exception("switch monitor %d failed", index)
        await self._send_monitors(ws, refresh=False)   # 成功与否都回一份，页面上的下拉框跟着当前值走

    async def _sender(self, ws):
        s = self.settings
        last_cursor = None
        idle = 0
        sent = nbytes = grabs = strips = keys = 0
        grab_ms = 0.0
        t_stat = time.monotonic()
        while True:
            if self.viewers <= 0:
                await asyncio.sleep(0.25)
                continue
            now = time.monotonic()
            while self.inflight and now - self.inflight[0] > ACK_TIMEOUT:
                self.inflight.popleft()
                if not self._ack_warned:
                    log.warning("relay did not ack within %.0fs; running without flow control (old relay.py?)", ACK_TIMEOUT)
                    self._ack_warned = True
            if len(self.inflight) >= MAX_INFLIGHT:
                await asyncio.sleep(0.005)   # 中继/观众还没消化完，先不截屏，变化会累积到下一帧
                continue
            t0 = now
            payload, w, h, cx, cy, key, n = await self.grabber.grab(s.snapshot())
            grab_ms += (time.monotonic() - t0) * 1000
            grabs += 1
            cursor = (cx, cy)
            if payload is not None:
                await ws.send_bytes(payload)
                self.inflight.append(time.monotonic())
                sent += 1
                nbytes += len(payload)
                strips += n
                keys += 1 if key else 0
                idle = 0
            else:
                idle += 1
                if cursor != last_cursor:
                    await ws.send_str(json.dumps({"t": "cur", "x": cx, "y": cy}))
            last_cursor = cursor
            now = time.monotonic()
            if now - t_stat >= 5:
                dt = now - t_stat
                mode = s.codec if not (s.codec == "h264" and self.grabber.enc is None) else "h264(starting)"
                if s.codec == "h264" and (not self.grabber.encoder or not self.grabber.h264_alive()):
                    mode = "jpeg(h264 unavailable)"
                log.info("%s %.1f fps  %.0f KB/s  (%.1f KB/frame, %.1f strips/frame, %d key)  grab+encode %.1f ms",
                         mode, sent / dt, nbytes / 1024 / dt, nbytes / 1024 / max(sent, 1), strips / max(sent, 1), keys,
                         grab_ms / max(grabs, 1))
                sent = nbytes = grabs = strips = keys = 0
                grab_ms = 0.0
                t_stat = now
            # 画面 2 秒没变就放慢到 5 次/秒轮询，省 CPU；一有变化立刻恢复满帧率。但刚收到过键鼠输入时不放慢，
            # 慢速等待中来了输入也立刻醒：否则看了一会儿再点，第一帧反应要多等最多 200 ms，光标也只剩 5 fps
            slow = idle >= s.fps * 2 and now - self.last_input > 2.0
            delay = (0.2 if slow else 1.0 / s.fps) - (now - t0)
            if delay <= 0:
                continue
            if not slow:
                await asyncio.sleep(delay)
                continue
            self.kick.clear()
            try:
                await asyncio.wait_for(self.kick.wait(), delay)
            except asyncio.TimeoutError:
                pass


def disable_quick_edit():
    """关掉本进程控制台的「快速编辑」：在窗口里点一下（远程点到也算）就不会进入选择状态。选择状态下往控制台写东西的线程
    会一直卡住——2026-09-24 就这么冻过：远程点到了 agent 自己的控制台，标题变成「选择 …」，agent 下一次写日志就卡死，
    不收消息、不回心跳、不重连，页面上只剩中继缓存的旧画面、怎么点都没反应。没有控制台（pythonw）时什么都不做。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = ctypes.c_void_p
    k32.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    k32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    h = k32.GetStdHandle(-10)          # STD_INPUT_HANDLE
    mode = ctypes.c_uint()
    if h and k32.GetConsoleMode(h, ctypes.byref(mode)):
        k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)   # 去掉 ENABLE_QUICK_EDIT_MODE，带上 ENABLE_EXTENDED_FLAGS 才生效


class ConsoleHandler(logging.Handler):
    """日志写控制台放到后台线程：控制台不管因为什么写不动（被选中、被暂停），卡住的只是这个线程，主循环照常跑；
    积压太多就丢，文件日志不受影响。"""

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


class TokenRedactingFormatter(logging.Formatter):
    """Keep authenticated URLs out of logs, including exception tracebacks."""

    def __init__(self, token):
        super().__init__("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
        self.redactions = sorted({token, quote(token, safe="")}, key=len, reverse=True)

    def format(self, record):
        text = super().format(record)
        # aiohttp/yarl may normalize the URL's percent encoding in exceptions.
        text = re.sub(r"([?&]token=)[^&#\s'\"<>]*", r"\1[redacted]", text, flags=re.IGNORECASE)
        for value in self.redactions:
            if value:
                text = text.replace(value, "[redacted]")
        return text


def setup_logging(path, token=""):
    fmt = TokenRedactingFormatter(token)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if sys.stderr is not None:
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(errors="replace")
        disable_quick_edit()
        h = ConsoleHandler(sys.stderr)
        h.setFormatter(fmt)
        root.addHandler(h)
    if path or sys.stderr is None:
        from logging.handlers import RotatingFileHandler
        p = path or str(Path(__file__).with_name("agent.log"))
        h = RotatingFileHandler(p, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
        h.setFormatter(fmt)
        root.addHandler(h)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--relay", required=True, help="ws://host:port 或 wss://host:port")
    ap.add_argument("--token", required=True, help="和 relay 一样的口令")
    ap.add_argument("--name", default=socket.gethostname(),
                    help="这台机器在页面上的名字（默认本机 hostname），一个中继挂多台时靠它区分")
    ap.add_argument("--cafile", help="relay 的自签证书 relay.crt（wss 时用它校验）")
    ap.add_argument("--insecure", action="store_true", help="wss 不校验证书（不推荐）")
    ap.add_argument("--monitor", type=int, default=1, help="一开始截哪个显示器（1、2…；0 = 全部拼一张），页面上可随时切")
    ap.add_argument("--codec", choices=["auto", "h264", "jpeg"], default="auto")
    ap.add_argument("--encoder", choices=["auto"] + ENCODER_ORDER, default="auto")
    ap.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg 路径，默认在 PATH 里找")
    ap.add_argument("--capture", choices=["auto", "dxcam", "mss"], default="auto", help="截屏方式：auto = 有 dxcam 就用 DXGI 桌面复制，否则 mss")
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--quality", type=int, default=60, help="jpeg 质量 10-95")
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--kbps", type=int, default=2000, help="h264 码率上限")
    ap.add_argument("--dry-run", action="store_true", help="只打印收到的输入事件，不注入")
    ap.add_argument("--log", help="日志文件路径")
    args = ap.parse_args()
    args.name = "".join(c for c in (args.name or "").strip() if ord(c) >= 32 and c != "\x7f")[:32] or "unnamed"
    setup_logging(args.log, args.token)
    if args.dry_run:
        log.info("DRY RUN: input events are logged, not injected")
    il = my_il()
    if il is not None and il < IL_HIGH:
        log.warning("not running as administrator (%s): whenever an elevated window (Task Manager, an app run as administrator, "
                    "an installer) is in the foreground, Windows drops this agent's mouse/keyboard input -- "
                    "start from an administrator terminal to avoid that", il_name(il))
    else:
        log.info("running as %s", il_name(il))
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)   # 系统定时器默认 15.6 ms 一格，asyncio.sleep(72ms) 会睡成 78–94 ms，12fps 只剩 10.7
    except Exception:
        pass

    encoder = None
    if args.codec != "jpeg":
        ffmpeg = shutil.which(args.ffmpeg)
        if not ffmpeg:
            log.warning("ffmpeg not found (%s): h264 unavailable, using jpeg", args.ffmpeg)
        else:
            args.ffmpeg = ffmpeg
            encoder = pick_encoder(ffmpeg, args.encoder)
            if encoder:
                log.info("h264 encoder: %s (%s)", encoder, ENCODERS[encoder][0])
            else:
                log.warning("no usable h264 encoder: using jpeg")
    try:
        asyncio.run(Agent(args, encoder).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
