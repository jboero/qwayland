#!/usr/bin/env python3
"""qwayland server: KDE Plasma (Wayland) virtual displays for Meta Quest.

Each panel the headset opens connects over TCP and asks for a display. The
server creates a real KWin virtual output of that size, captures it through
PipeWire, encodes with NVENC (H.264) and streams it back. Pointer and keyboard
input from the headset is injected back into KWin. The virtual output vanishes
when the panel disconnects.

Wire protocol (little-endian), every message is: u8 type, u32 length, payload.
  client -> server
    0x10 HELLO     JSON {"proto": 1, "width": int, "height": int, "scale": float (optional)}
    0x11 POINTER   f32 nx, f32 ny             normalized position on the display
    0x12 BUTTON    u32 evdev code, u8 pressed
    0x13 SCROLL    u8 axis (0 vertical, 1 horizontal), f32 value
    0x14 KEY       u32 evdev code, u8 pressed
    0x15 KEYFRAME  (empty)
    0x16 PING      u64 client timestamp
    0x17 TEXT      UTF-8 text typed on a virtual keyboard
  server -> client
    0x01 CONFIG    JSON {"width": int, "height": int, "codec": "h264", "name": str}
    0x02 VIDEO     u64 pts_us, u8 flags (bit0 keyframe), Annex-B access unit
    0x03 PONG      u64 echoed client timestamp
    0x04 ERROR     UTF-8 message
"""

import argparse
import asyncio
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

log = logging.getLogger("qwayland")

HERE = os.path.dirname(os.path.realpath(__file__))
VOUT_HELPER = os.path.join(HERE, "vout", "qw-vout")

MSG_CONFIG, MSG_VIDEO, MSG_PONG, MSG_ERROR = 0x01, 0x02, 0x03, 0x04
MSG_HELLO, MSG_POINTER, MSG_BUTTON, MSG_SCROLL, MSG_KEY, MSG_KEYFRAME, MSG_PING, MSG_TEXT = range(0x10, 0x18)

HEADER = struct.Struct("<BI")
MAX_PAYLOAD = 64 * 1024 * 1024


class VoutHelper:
    """Drives the qw-vout helper process (virtual outputs + input injection)."""

    def __init__(self):
        self.proc = None
        self.waiters = {}  # (event, id) -> Future
        self.geometry = {}
        self.ready = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            VOUT_HELPER, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
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
            await self._apply_scale(vid, scale)
        except asyncio.TimeoutError:
            log.warning("no geometry for display %d; input mapping may be off", vid)
        return node

    async def _apply_scale(self, vid, scale):
        # KScreen remembers per-output settings by name and may pick its own
        # scale for a new virtual output, so set the requested one explicitly.
        name = self.geometry[vid][4]
        if not shutil.which("kscreen-doctor"):
            return
        proc = await asyncio.create_subprocess_exec(
            "kscreen-doctor", f"output.{name}.scale.{scale:g}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()

    def close(self, vid):
        self.geometry.pop(vid, None)
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


SPECIAL_KEYSYMS = {"\n": 0xFF0D, "\r": 0xFF0D, "\t": 0xFF09, "\b": 0xFF08, "\x1b": 0xFF1B}


def keysym_for(ch):
    """XKB keysym for a character (Latin-1 maps directly, the rest via Unicode)."""
    if ch in SPECIAL_KEYSYMS:
        return SPECIAL_KEYSYMS[ch]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp


class Encoder:
    """PipeWire node -> NVENC H.264 -> callback with Annex-B access units."""

    def __init__(self, node, width, height, bitrate_kbps, fps, on_frame):
        self.on_frame = on_frame
        encoder = self._pick_encoder(bitrate_kbps, fps)
        desc = (
            f"pipewiresrc path={node} do-timestamp=true keepalive-time=1000 always-copy=false ! "
            f"videorate drop-only=true max-rate={fps} ! "
            f"{self._convert()} ! "
            f"{encoder} ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink emit-signals=true sync=false max-buffers=4 drop=true"
        )
        log.debug("pipeline: %s", desc)
        self.pipeline = Gst.parse_launch(desc)
        self.sink = self.pipeline.get_by_name("sink")
        self.sink.connect("new-sample", self._on_sample)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_error)

    @staticmethod
    def _convert():
        # Colour conversion is the most expensive step at 1440p; do it on the
        # GPU when the CUDA elements are present.
        if Gst.ElementFactory.find("cudaupload") and Gst.ElementFactory.find("cudaconvert"):
            return "cudaupload ! cudaconvert ! video/x-raw(memory:CUDAMemory),format=NV12"
        return "videoconvert n-threads=4 ! video/x-raw,format=NV12"

    @staticmethod
    def _pick_encoder(bitrate_kbps, fps):
        if Gst.ElementFactory.find("nvh264enc"):
            return (
                "nvh264enc preset=low-latency-hq zerolatency=true bframes=0 rc-mode=cbr "
                f"bitrate={bitrate_kbps} gop-size={fps * 4} aud=false ! video/x-h264,profile=high"
            )
        log.warning("nvh264enc not available, falling back to x264enc (CPU)")
        return (
            f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={bitrate_kbps} "
            f"key-int-max={fps * 4} ! video/x-h264,profile=high"
        )

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            keyframe = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
            pts = buf.pts // 1000 if buf.pts != Gst.CLOCK_TIME_NONE else 0
            self.on_frame(pts, keyframe, bytes(info.data))
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def _on_error(self, bus, msg):
        err, dbg = msg.parse_error()
        log.error("pipeline error: %s (%s)", err.message, dbg)

    def start(self):
        self.pipeline.set_state(Gst.State.PLAYING)

    def stop(self):
        self.pipeline.set_state(Gst.State.NULL)

    def request_keyframe(self):
        event = GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0)
        self.sink.send_event(event)


class Session:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.vid = None
        self.encoder = None
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
        width, height = width - width % 2, height - height % 2
        self.vid = self.server.allocate_id()
        log.info("%s: creating virtual display %d (%dx%d)", self.peer, self.vid, width, height)

        scale = float(hello.get("scale") or args.scale)
        node = await self.server.vout.create(self.vid, width, height, scale)
        self.encoder = Encoder(node, width, height, args.bitrate, args.fps, self._on_frame)
        config = {"width": width, "height": height, "codec": "h264",
                  "name": f"{socket.gethostname()} #{self.vid}"}
        self.send(MSG_CONFIG, json.dumps(config).encode())
        self.encoder.start()

        pump = asyncio.create_task(self._pump_video())
        try:
            await self._handle_input(pump)
        finally:
            pump.cancel()

    async def _handle_input(self, pump):
        vout = self.server.vout
        while not pump.done():
            mtype, payload = await self.read_msg()
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
                self.encoder.request_keyframe()
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
        self.sessions = set()
        self.loop = None
        self.avahi = None

    def allocate_id(self):
        vid = 1
        while vid in self.ids:
            vid += 1
        self.ids.add(vid)
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
        if not shutil.which("avahi-publish-service"):
            log.warning("avahi-publish-service not found; headset must connect by IP")
            return
        name = self.args.name or socket.gethostname()
        self.avahi = await asyncio.create_subprocess_exec(
            "avahi-publish-service", name, "_qwayland._tcp", str(self.args.port), "proto=1",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )

    async def run(self):
        self.loop = asyncio.get_running_loop()
        await self.vout.start()
        server = await asyncio.start_server(self.handle, host=self.args.bind, port=self.args.port)
        await self.advertise()
        log.info("listening on %s:%d", self.args.bind, self.args.port)
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
    p.add_argument("--width", type=int, default=2560, help="default display width")
    p.add_argument("--height", type=int, default=1440, help="default display height")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--bitrate", type=int, default=40000, help="kbit/s per display")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    Gst.init(None)
    # GStreamer bus watches are dispatched from a GLib main loop.
    threading.Thread(target=GLib.MainLoop().run, daemon=True).start()
    asyncio.run(Server(args).run())


if __name__ == "__main__":
    main()
