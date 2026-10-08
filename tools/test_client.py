#!/usr/bin/env python3
"""Headless qwayland test client: requests a display, saves the H.264 stream."""
import argparse, json, socket, struct, time

p = argparse.ArgumentParser()
p.add_argument("host"); p.add_argument("--port", type=int, default=7710)
p.add_argument("--width", type=int, default=1920); p.add_argument("--height", type=int, default=1080)
p.add_argument("--seconds", type=float, default=5); p.add_argument("--out", default="out.bin")
p.add_argument("--codecs", default="av1,h265,h264", help="codecs to offer ('' = old client)")
p.add_argument("--reject", help="report DECODER_ERROR when this codec is chosen")
a = p.parse_args()

s = socket.create_connection((a.host, a.port))
def send(t, payload=b""): s.sendall(struct.pack("<BI", t, len(payload)) + payload)
def recv_exact(n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c: raise EOFError
        b += c
    return b
hello = {"proto": 1, "width": a.width, "height": a.height}
if a.codecs:
    hello["codecs"] = a.codecs.split(",")
send(0x10, json.dumps(hello).encode())
frames = keyframes = nbytes = 0
start = time.time(); first = None
f = open(a.out, "wb")
if True:
    while time.time() - start < a.seconds:
        t, n = struct.unpack("<BI", recv_exact(5)); payload = recv_exact(n)
        if t == 0x01:
            print("config", payload.decode())
            codec = json.loads(payload)["codec"]
            f.close(); f = open(f"{a.out}.{codec}", "wb")
            if codec == a.reject:
                send(0x18, b"test client refuses this codec")
        elif t == 0x04: print("error", payload.decode()); break
        elif t == 0x02:
            pts, flags = struct.unpack("<QB", payload[:9])
            first = first or time.time()
            frames += 1; keyframes += flags & 1; nbytes += n - 9
            f.write(payload[9:])
            if frames == 30:  # exercise input path: move pointer to centre
                send(0x11, struct.pack("<ff", 0.5, 0.5))
print(f"frames={frames} keyframes={keyframes} bytes={nbytes} first_frame_after={first-start if first else None:.3f}s")
