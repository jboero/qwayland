#!/usr/bin/env python3
"""Headless qwayland test client: requests a display, saves the H.264 stream."""
import argparse, json, socket, struct, time

p = argparse.ArgumentParser()
p.add_argument("host"); p.add_argument("--port", type=int, default=7710)
p.add_argument("--width", type=int, default=1920); p.add_argument("--height", type=int, default=1080)
p.add_argument("--seconds", type=float, default=5); p.add_argument("--out", default="out.h264")
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
send(0x10, json.dumps({"proto": 1, "width": a.width, "height": a.height}).encode())
frames = keyframes = nbytes = 0
start = time.time(); first = None
with open(a.out, "wb") as f:
    while time.time() - start < a.seconds:
        t, n = struct.unpack("<BI", recv_exact(5)); payload = recv_exact(n)
        if t == 0x01: print("config", payload.decode())
        elif t == 0x04: print("error", payload.decode()); break
        elif t == 0x02:
            pts, flags = struct.unpack("<QB", payload[:9])
            first = first or time.time()
            frames += 1; keyframes += flags & 1; nbytes += n - 9
            f.write(payload[9:])
            if frames == 30:  # exercise input path: move pointer to centre
                send(0x11, struct.pack("<ff", 0.5, 0.5))
print(f"frames={frames} keyframes={keyframes} bytes={nbytes} first_frame_after={first-start if first else None:.3f}s")
