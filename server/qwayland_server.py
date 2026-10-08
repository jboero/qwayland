#!/usr/bin/env python3
"""qwayland server: KDE Plasma (Wayland) virtual displays for Meta Quest.

Each panel the headset opens connects over TCP and asks for a display. The
server creates a real KWin virtual output of that size, captures it through
PipeWire, encodes it on the GPU (AV1, H.265 or H.264, negotiated with the
headset, falling back down that list) and streams it back. Pointer and keyboard
input from the headset is injected back into KWin. The virtual output vanishes
when the panel disconnects.

Wire protocol (little-endian), every message is: u8 type, u32 length, payload.
  client -> server
    0x10 HELLO     JSON {"proto": 1, "width": int, "height": int,
                         "scale": float (optional), "panel": str (optional, stable panel id),
                         "codecs": [str] (optional, decodable codecs: "av1", "h265", "h264";
                                    absent means h264 only)}
    0x11 POINTER   f32 nx, f32 ny             normalized position on the display
    0x12 BUTTON    u32 evdev code, u8 pressed
    0x13 SCROLL    u8 axis (0 vertical, 1 horizontal), f32 value
    0x14 KEY       u32 evdev code, u8 pressed
    0x15 KEYFRAME  (empty)
    0x16 PING      u64 client timestamp
    0x17 TEXT      UTF-8 text typed on a virtual keyboard
    0x18 DECODER_ERROR  UTF-8 reason; the server falls back to its next codec
  server -> client
    0x01 CONFIG    JSON {"width": int, "height": int, "codec": "av1"|"h265"|"h264", "name": str}
                   (sent again if the codec changes mid-session)
    0x02 VIDEO     u64 pts_us, u8 flags (bit0 keyframe), then an Annex-B access unit
                   (H.264/H.265) or an AV1 temporal unit (low-overhead OBU format)
    0x03 PONG      u64 echoed client timestamp
    0x04 ERROR     UTF-8 message
"""

import argparse
import asyncio
import ctypes
import fractions
import json
import logging
import os
import shutil
import signal
import socket
import struct
import threading

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

# Optional: PyAV gives access to FFmpeg's NVENC AV1 encoder when GStreamer
# has no nvav1enc element. Without it the server simply doesn't offer AV1.
try:
    import av
    import numpy as np
except ImportError:
    av = np = None

log = logging.getLogger("qwayland")

HERE = os.path.dirname(os.path.realpath(__file__))
VOUT_HELPER = os.path.join(HERE, "vout", "qw-vout")

MSG_CONFIG, MSG_VIDEO, MSG_PONG, MSG_ERROR = 0x01, 0x02, 0x03, 0x04
(MSG_HELLO, MSG_POINTER, MSG_BUTTON, MSG_SCROLL, MSG_KEY, MSG_KEYFRAME, MSG_PING, MSG_TEXT,
 MSG_DECODER_ERROR) = range(0x10, 0x19)

HEADER = struct.Struct("<BI")
MAX_PAYLOAD = 64 * 1024 * 1024
# A live panel pings every few seconds. If the headset goes quiet (asleep,
# frozen, out of range) drop its display rather than leaving a monitor nobody
# can see attached to the desktop.
CLIENT_TIMEOUT = 20


def _die_with_parent():
    """Child processes (helper, mDNS advertiser) must not outlive the server."""
    PR_SET_PDEATHSIG = 1
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGTERM)


class VoutHelper:
    """Drives the qw-vout helper process (virtual outputs + input injection)."""

    def __init__(self):
        self.proc = None
        self.waiters = {}  # (event, id) -> Future
        self.geometry = {}
        self.ready = None
        # Displays created concurrently (e.g. panels reconnecting together)
        # must not compute their positions from each other's stale layout.
        self.layout_lock = asyncio.Lock()

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            VOUT_HELPER, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            preexec_fn=_die_with_parent,
        )
        self.ready = asyncio.get_running_loop().create_future()
        asyncio.create_task(self._read_events())
        await asyncio.wait_for(self.ready, 5)

    async def _read_events(self):
        while line := await self.proc.stdout.readline():
            parts = line.decode().strip().split(" ", 2)
            event = parts[0]
            log.debug("vout: %s", line.decode().strip())
            if event == "ready":
                self.ready.set_result(True)
            elif event == "error":
                log.warning("vout helper: %s", " ".join(parts[1:]))
                if not self.ready.done():
                    self.ready.set_exception(RuntimeError(" ".join(parts[1:])))
            elif event in ("node", "failed", "closed", "geom"):
                vid = int(parts[1])
                rest = parts[2] if len(parts) > 2 else ""
                if event == "geom":
                    x, y, w, h, name = rest.split(" ", 4)
                    self.geometry[vid] = (int(x), int(y), int(w), int(h), name)
                fut = self.waiters.pop((event, vid), None)
                if event == "failed":
                    fut = self.waiters.pop(("node", vid), fut)
                    if fut and not fut.done():
                        fut.set_exception(RuntimeError(rest))
                elif fut and not fut.done():
                    fut.set_result(rest)
        log.error("vout helper exited")
        for fut in self.waiters.values():
            if not fut.done():
                fut.set_exception(RuntimeError("vout helper exited"))

    def _send(self, line):
        self.proc.stdin.write((line + "\n").encode())

    async def create(self, vid, width, height, scale):
        loop = asyncio.get_running_loop()
        node = loop.create_future()
        geom = loop.create_future()
        self.waiters[("node", vid)] = node
        self.waiters[("geom", vid)] = geom
        self._send(f"create {vid} {width} {height} {scale}")
        node = int(await asyncio.wait_for(node, 5))
        try:
            await asyncio.wait_for(geom, 2)
            await self.relayout({vid: scale})
        except asyncio.TimeoutError:
            log.warning("no geometry for display %d; input mapping may be off", vid)
        return node

    async def relayout(self, scales=None):
        """Pack all qwayland displays, in id order, right of the real monitors.

        KScreen remembers per-output settings by name and lays new outputs out
        from possibly stale geometry, so positions (and the requested scale of
        new displays) are always set explicitly.
        """
        if not shutil.which("kscreen-doctor"):
            return
        async with self.layout_lock:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "kscreen-doctor", "-j", stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
                outputs = json.loads((await proc.communicate())[0])["outputs"]
            except (ValueError, KeyError) as e:
                log.warning("cannot read screen layout: %s", e)
                return
            scales = scales or {}
            ours = {v[4]: vid for vid, v in self.geometry.items()}
            right, virtual = 0, []
            for o in outputs:
                if not o.get("enabled"):
                    continue
                if o.get("name") in ours:
                    virtual.append(o)
                    continue
                right = max(right, o["pos"]["x"] + _logical_width(o))
            args = []
            for o in sorted(virtual, key=lambda o: ours[o["name"]]):
                name, vid = o["name"], ours[o["name"]]
                scale = scales.get(vid, o.get("scale") or 1)
                if vid in scales:
                    args.append(f"output.{name}.scale.{scale:g}")
                args.append(f"output.{name}.position.{right},0")
                right += _logical_width(o, scale)
            if args:
                proc = await asyncio.create_subprocess_exec(
                    "kscreen-doctor", *args,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()

    def close(self, vid):
        if self.geometry.pop(vid, None):
            asyncio.get_running_loop().create_task(self.relayout())
        self._send(f"close {vid}")

    def motion(self, vid, nx, ny):
        self._send(f"motion {vid} {nx:.5f} {ny:.5f}")

    def button(self, code, pressed):
        self._send(f"button {code} {1 if pressed else 0}")

    def scroll(self, axis, value):
        self._send(f"axis {axis} {value:.3f}")

    def key(self, code, pressed):
        self._send(f"key {code} {1 if pressed else 0}")

    def text(self, text):
        for ch in text:
            sym = keysym_for(ch)
            self._send(f"keysym {sym} 1")
            self._send(f"keysym {sym} 0")


def _logical_width(output, scale=None):
    """Width of a KScreen output in global (logical) coordinates."""
    mode = next((m for m in output.get("modes", []) if m["id"] == output.get("currentModeId")), None)
    size = mode["size"] if mode else output.get("size", {"width": 0, "height": 0})
    # KScreen rotation enum: 2 = left (90 degrees), 8 = right (270 degrees)
    width = size["height"] if output.get("rotation") in (2, 8) else size["width"]
    return round(width / (scale or output.get("scale") or 1))


SPECIAL_KEYSYMS = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08, "\x1b": 0xFF1B}


def keysym_for(ch):
    """XKB keysym for a character (Latin-1 maps directly, the rest via Unicode)."""
    if ch in SPECIAL_KEYSYMS:
        return SPECIAL_KEYSYMS[ch]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


# Codecs in the server's default order of preference. AV1 needs roughly half
# the bitrate of H.264 for the same quality on desktop content.
CODECS = ("av1", "h265", "h264")
# Default CBR bitrates (kbit/s) for 2560x1440 at 60 fps, scaled by pixel count.
DEFAULT_BITRATE = {"av1": 20000, "h265": 28000, "h264": 40000}


def init_cuda():
    """GStreamer's nvcodec plugin only registers some encoders (nvav1enc) once
    CUDA has been initialised in the process, so do that before probing."""
    if not Gst.ElementFactory.find("cudaupload"):
        return
    pipeline = Gst.parse_launch("videotestsrc num-buffers=1 ! cudaupload ! fakesink")
    pipeline.set_state(Gst.State.PLAYING)
    pipeline.get_bus().timed_pop_filtered(2 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    pipeline.set_state(Gst.State.NULL)


def default_bitrate(codec, width, height):
    return int(DEFAULT_BITRATE[codec] * width * height / (2560 * 1440))


def encoder_backend(codec):
    """How this machine can encode `codec`: 'gst', 'pyav' or None."""
    gst_element = {"h264": "nvh264enc", "h265": "nvh265enc", "av1": "nvav1enc"}[codec]
    if Gst.ElementFactory.find(gst_element):
        return "gst"
    if av is not None and np is not None and f"{_PYAV_NAMES[codec]}" in av.codecs_available:
        return "pyav"
    if codec == "h264" and Gst.ElementFactory.find("x264enc"):
        return "gst"
    return None


_PYAV_NAMES = {"h264": "h264_nvenc", "h265": "hevc_nvenc", "av1": "av1_nvenc"}


class EncoderError(RuntimeError):
    pass


class Encoder:
    """PipeWire node -> GPU colour conversion -> hardware encoder -> on_frame.

    H.264/H.265 (and AV1 where GStreamer has nvav1enc) are encoded inside the
    GStreamer pipeline. Otherwise AV1 goes through PyAV's av1_nvenc: the
    pipeline then ends in raw NV12 frames that are encoded in the appsink
    callback. Output is Annex-B access units (H.26x) or AV1 temporal units.
    """

    def __init__(self, codec, node, width, height, bitrate_kbps, fps, on_frame, on_error):
        self.codec = codec
        self.width, self.height, self.fps = width, height, fps
        self.on_frame = on_frame
        self.on_error = on_error
        self.av_ctx = None
        self.force_keyframe = False
        self.backend = encoder_backend(codec)
        if self.backend is None:
            raise EncoderError(f"no {codec} encoder available")

        if self.backend == "gst":
            tail = f"{self._convert()} ! {self._gst_encoder(codec, bitrate_kbps, fps)}"
        else:
            self.av_ctx = self._open_pyav(codec, width, height, bitrate_kbps, fps)
            tail = self._convert_to_system_memory()
        desc = (
            f"pipewiresrc path={node} do-timestamp=true keepalive-time=1000 always-copy=false ! "
            f"videorate drop-only=true max-rate={fps} ! {tail} ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=4 drop=true"
        )
        log.debug("pipeline (%s via %s): %s", codec, self.backend, desc)
        self.pipeline = Gst.parse_launch(desc)
        self.sink = self.pipeline.get_by_name("sink")
        self.sink.connect("new-sample", self._on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error)

    @staticmethod
    def _convert():
        # Colour conversion is the most expensive step at 1440p; do it on the
        # GPU when the CUDA elements are present.
        if Gst.ElementFactory.find("cudaupload") and Gst.ElementFactory.find("cudaconvert"):
            return "cudaupload ! cudaconvert ! video/x-raw(memory:CUDAMemory),format=NV12"
        return "videoconvert n-threads=4 ! video/x-raw,format=NV12"

    @staticmethod
    def _convert_to_system_memory():
        if Gst.ElementFactory.find("cudadownload") and Gst.ElementFactory.find("cudaconvert"):
            return ("cudaupload ! cudaconvert ! video/x-raw(memory:CUDAMemory),format=NV12 ! "
                    "cudadownload ! video/x-raw,format=NV12")
        return "videoconvert n-threads=4 ! video/x-raw,format=NV12"

    @staticmethod
    def _gst_encoder(codec, bitrate_kbps, fps):
        nv = ("preset=p1 tune=ultra-low-latency zerolatency=true bframes=0 rc-mode=cbr "
              f"bitrate={bitrate_kbps} gop-size={fps * 4}")
        if codec == "h265":
            return (f"nvh265enc {nv} aud=false ! h265parse config-interval=-1 ! "
                    "video/x-h265,stream-format=byte-stream,alignment=au")
        if codec == "av1":
            return (f"nvav1enc {nv} ! av1parse ! "
                    "video/x-av1,stream-format=obu-stream,alignment=tu")
        if Gst.ElementFactory.find("nvh264enc"):
            enc = f"nvh264enc {nv} aud=false ! video/x-h264,profile=high"
        else:
            log.warning("nvh264enc not available, falling back to x264enc (CPU)")
            enc = (f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
                   f"key-int-max={fps * 4} ! video/x-h264,profile=high")
        return (f"{enc} ! h264parse config-interval=-1 ! "
                "video/x-h264,stream-format=byte-stream,alignment=au")

    @staticmethod
    def _open_pyav(codec, width, height, bitrate_kbps, fps):
        ctx = av.CodecContext.create(_PYAV_NAMES[codec], "w")
        ctx.width, ctx.height, ctx.pix_fmt = width, height, "nv12"
        ctx.time_base = fractions.Fraction(1, 1_000_000)  # pts in microseconds
        ctx.framerate = fractions.Fraction(fps, 1)
        ctx.bit_rate = bitrate_kbps * 1000
        ctx.options = {
            "preset": "p1", "tune": "ull", "rc": "cbr", "delay": "0", "zerolatency": "1",
            "g": str(fps * 4), "forced-idr": "1",
            "maxrate": str(bitrate_kbps * 1000), "bufsize": str(bitrate_kbps * 1000 // fps * 2),
        }
        try:
            ctx.open()
        except Exception as e:
            raise EncoderError(f"{_PYAV_NAMES[codec]}: {e}") from e
        return ctx

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        pts = buf.pts // 1000 if buf.pts != Gst.CLOCK_TIME_NONE else 0
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.OK
        try:
            if self.av_ctx is None:
                keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
                self.on_frame(pts, keyframe, bytes(info.data))
            else:
                self._encode_pyav(sample.get_caps(), info.data, pts)
        except Exception as e:
            self.on_error(f"{self.codec} encode failed: {e}")
            return Gst.FlowReturn.ERROR
        finally:
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def _encode_pyav(self, caps, data, pts):
        vinfo = GstVideo.VideoInfo.new_from_caps(caps)
        raw = np.frombuffer(data, np.uint8)
        frame = av.VideoFrame(self.width, self.height, "nv12")
        for plane, rows in ((0, self.height), (1, self.height // 2)):
            stride, offset = vinfo.stride[plane], vinfo.offset[plane]
            src = raw[offset:offset + stride * rows].reshape(rows, stride)
            dst = np.zeros((rows, frame.planes[plane].line_size), np.uint8)
            dst[:, :self.width] = src[:, :self.width]
            frame.planes[plane].update(dst)
        frame.pts = pts
        if self.force_keyframe:
            frame.pict_type = av.video.frame.PictureType.I
            self.force_keyframe = False
        for packet in self.av_ctx.encode(frame):
            self.on_frame(packet.pts or pts, packet.is_keyframe, bytes(packet))

    def _on_bus_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log.debug("pipeline error detail: %s", dbg)
        self.on_error(f"{self.codec} pipeline error: {err.message}")

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise EncoderError(f"{self.codec} pipeline failed to start")

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    def request_keyframe(self):
        if self.av_ctx is not None:
            self.force_keyframe = True
            return
        event = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
        self.sink.send_event(event)


class Session:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.vid = None
        self.node = None
        self.size = None
        self.encoder = None
        self.candidates = []
        self.got_frame = False
        self.frames = asyncio.Queue(maxsize=8)
        self.need_keyframe = True
        self.peer = writer.get_extra_info("peername")

    def send(self, mtype, payload=b""):
        self.writer.write(HEADER.pack(mtype, len(payload)) + payload)

    async def read_msg(self):
        mtype, length = HEADER.unpack(await self.reader.readexactly(HEADER.size))
        if length > MAX_PAYLOAD:
            raise ValueError(f"message too large: {length}")
        return mtype, await self.reader.readexactly(length)

    def _on_frame(self, pts, keyframe, data):
        # Called from the GStreamer streaming thread.
        self.server.loop.call_soon_threadsafe(self._enqueue, pts, keyframe, data)

    def _enqueue(self, pts, keyframe, data):
        if self.need_keyframe and not keyframe:
            return
        try:
            self.frames.put_nowait((pts, keyframe, data))
            self.need_keyframe = False
            self.got_frame = True
        except asyncio.QueueFull:
            # Network can't keep up: drop until the next keyframe.
            while not self.frames.empty():
                self.frames.get_nowait()
            self.need_keyframe = True
            if self.encoder:
                self.encoder.request_keyframe()

    async def _pump_video(self):
        while True:
            pts, keyframe, data = await self.frames.get()
            self.send(MSG_VIDEO, struct.pack("<QB", pts, 1 if keyframe else 0) + data)
            await self.writer.drain()

    async def run(self):
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        mtype, payload = await asyncio.wait_for(self.read_msg(), 10)
        if mtype != MSG_HELLO:
            raise ValueError("expected HELLO")
        hello = json.loads(payload)
        args = self.server.args
        width = int(hello.get("width") or args.width)
        height = int(hello.get("height") or args.height)
        # Keep within what NVENC H.264 and the headset decoder handle.
        width = min(max(width, 320), 4096)
        height = min(max(height, 240), 4096)
        width, height = width - width % 2, height - height % 2
        self.vid = self.server.allocate_id(hello.get("panel"))
        log.info("%s: creating virtual display %d (%dx%d)", self.peer, self.vid, width, height)

        scale = min(max(float(hello.get("scale") or args.scale), 0.5), 4.0)
        self.node = await self.server.vout.create(self.vid, width, height, scale)
        self.size = (width, height)

        # Clients from before codec negotiation only decode H.264.
        offered = hello.get("codecs") or ["h264"]
        self.candidates = [c for c in args.codecs
                           if c in offered and c not in self.server.broken_codecs]
        self._start_next_encoder()

        pump = asyncio.create_task(self._pump_video())
        try:
            await self._handle_input(pump)
        finally:
            pump.cancel()

    def _start_next_encoder(self, reason=None):
        """Start the most preferred remaining codec; falls back down the list."""
        if self.encoder:
            self.encoder.stop()
            self.encoder = None
        args = self.server.args
        width, height = self.size
        while self.candidates:
            codec = self.candidates.pop(0)
            bitrate = args.bitrate or default_bitrate(codec, width, height)
            try:
                encoder = Encoder(codec, self.node, width, height, bitrate, args.fps,
                                  self._on_frame, self._on_encoder_error)
                encoder.start()
            except (EncoderError, GLib.Error) as e:
                log.warning("%s: %s; trying next codec", self.peer, e)
                self.server.broken_codecs.add(codec)
                continue
            self.encoder = encoder
            self.got_frame = False
            self.need_keyframe = True
            while not self.frames.empty():
                self.frames.get_nowait()
            log.info("%s: display %d streaming %s (%s) at %d kbit/s", self.peer, self.vid,
                     codec, encoder.backend, bitrate)
            config = {"width": width, "height": height, "codec": codec,
                      "name": f"{socket.gethostname()} #{self.vid}"}
            self.send(MSG_CONFIG, json.dumps(config).encode())
            return
        message = "no video codec in common with the headset"
        if reason:
            message += f" (last error: {reason})"
        raise EncoderError(message)

    def _on_encoder_error(self, message):
        # Called from GStreamer threads.
        self.server.loop.call_soon_threadsafe(self._fall_back, message, True)

    def _fall_back(self, reason, encoder_side):
        if self.encoder is None:
            return
        codec = self.encoder.codec
        log.warning("%s: %s; falling back from %s", self.peer, reason, codec)
        if encoder_side and not self.got_frame:
            # Failed before producing anything: treat as broken on this machine.
            self.server.broken_codecs.add(codec)
        try:
            self._start_next_encoder(reason)
        except EncoderError as e:
            log.error("%s: %s", self.peer, e)
            self.send(MSG_ERROR, str(e).encode())
            self.writer.close()

    async def _handle_input(self, pump):
        vout = self.server.vout
        while not pump.done():
            try:
                mtype, payload = await asyncio.wait_for(self.read_msg(), CLIENT_TIMEOUT)
            except asyncio.TimeoutError:
                log.info("%s: no messages for %ds, closing display", self.peer, CLIENT_TIMEOUT)
                return
            if log.isEnabledFor(logging.DEBUG) and mtype != MSG_POINTER:
                log.debug("%s: input 0x%02x %s", self.peer, mtype, payload.hex())
            if mtype == MSG_POINTER:
                nx, ny = struct.unpack("<ff", payload)
                vout.motion(self.vid, nx, ny)
            elif mtype == MSG_BUTTON:
                code, pressed = struct.unpack("<IB", payload)
                vout.button(code, pressed)
            elif mtype == MSG_SCROLL:
                axis, value = struct.unpack("<Bf", payload)
                vout.scroll(axis, value)
            elif mtype == MSG_KEY:
                code, pressed = struct.unpack("<IB", payload)
                vout.key(code, pressed)
            elif mtype == MSG_TEXT:
                vout.text(payload.decode("utf-8", "replace"))
            elif mtype == MSG_KEYFRAME:
                if self.encoder:
                    self.encoder.request_keyframe()
            elif mtype == MSG_DECODER_ERROR:
                self._fall_back("headset cannot decode: " + payload.decode("utf-8", "replace"), False)
            elif mtype == MSG_PING:
                self.send(MSG_PONG, payload)
            else:
                log.debug("ignoring message type 0x%02x", mtype)

    def close(self):
        if self.encoder:
            self.encoder.stop()
            self.encoder = None
        if self.vid is not None:
            self.server.vout.close(self.vid)
            self.server.release_id(self.vid)
            self.vid = None
        self.writer.close()


class Server:
    def __init__(self, args):
        self.args = args
        self.vout = VoutHelper()
        self.ids = set()
        self.panels = {}  # stable panel id -> display id it used last
        self.broken_codecs = set()  # encoders that failed to initialise here
        self.sessions = set()
        self.loop = None
        self.avahi = None

    def allocate_id(self, panel=None):
        # Give a reconnecting panel its previous display number so it keeps
        # the same output name (and KScreen settings) and position.
        vid = self.panels.get(panel)
        if vid is None or vid in self.ids:
            vid = 1
            while vid in self.ids or (vid in self.panels.values() and panel not in self.panels):
                vid += 1
        self.ids.add(vid)
        if panel:
            self.panels[panel] = vid
        return vid

    def release_id(self, vid):
        self.ids.discard(vid)

    async def handle(self, reader, writer):
        session = Session(self, reader, writer)
        self.sessions.add(session)
        log.info("%s: connected", session.peer)
        try:
            await session.run()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception as e:  # report to the headset, keep serving others
            log.exception("%s: session failed", session.peer)
            try:
                session.send(MSG_ERROR, str(e).encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            self.sessions.discard(session)
            session.close()
            log.info("%s: disconnected", session.peer)

    async def advertise(self):
        if self.args.no_advertise:
            return
        if not shutil.which("avahi-publish-service"):
            log.warning("avahi-publish-service not found; headset must connect by IP")
            return
        name = self.args.name or socket.gethostname()
        self.avahi = await asyncio.create_subprocess_exec(
            "avahi-publish-service", name, "_qwayland._tcp", str(self.args.port), "proto=1",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )

    async def run(self):
        self.loop = asyncio.get_running_loop()
        await self.vout.start()
        server = await asyncio.start_server(self.handle, host=self.args.bind, port=self.args.port)
        await self.advertise()
        log.info("listening on %s:%d", self.args.bind, self.args.port)
        log.info("encoders: %s", ", ".join(f"{c}={encoder_backend(c) or 'unavailable'}"
                                             for c in self.args.codecs))
        stop = self.loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, stop.set_result, None)
        await stop
        log.info("shutting down")
        server.close()
        # Python >= 3.12 waits for open connections in wait_closed(), so close
        # the sessions (and with them the virtual outputs) ourselves.
        for session in list(self.sessions):
            session.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 2)
        except asyncio.TimeoutError:
            pass
        if self.avahi:
            self.avahi.terminate()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=7710)
    p.add_argument("--name", help="name advertised over mDNS (default: hostname)")
    p.add_argument("--no-advertise", action="store_true", help="don't publish over mDNS")
    p.add_argument("--width", type=int, default=2560, help="default display width")
    p.add_argument("--height", type=int, default=1440, help="default display height")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--bitrate", type=int,
                   help="kbit/s per display (default: per codec, scaled by resolution)")
    p.add_argument("--codecs", default=",".join(CODECS),
                   help="codec preference order, comma separated (default: %(default)s)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    args.codecs = [c.strip() for c in args.codecs.split(",") if c.strip()]
    unknown = set(args.codecs) - set(CODECS)
    if unknown:
        p.error(f"unknown codecs: {', '.join(sorted(unknown))}")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    Gst.init(None)
    init_cuda()
    # GStreamer bus watches are dispatched from a GLib main loop.
    threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
    asyncio.run(Server(args).run())


if __name__ == "__main__":
    main()
