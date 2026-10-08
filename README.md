# qwayland

Extra monitors for your KDE Plasma (Wayland) desktop, floating in a Meta Quest.

<img width="3840" height="2160" alt="com oculus vrshell-20261008-131706" src="https://github.com/user-attachments/assets/52df1e31-28da-424b-8c4b-b3fff4cac62d" />

Each panel you open in the headset becomes a real, separate monitor on the
Linux desktop: KWin creates a virtual output of the requested size, so windows
can be dragged onto it, maximized on it, and arranged in System Settings like any
other screen. Close the panel and the monitor disappears again.

```
 Linux PC (KDE Plasma, Wayland)                         Meta Quest
┌────────────────────────────────────────┐            ┌─────────────────────┐
│ KWin virtual output "Virtual-qwayland-1"│            │ qwayland panel app  │
│        │ PipeWire (zkde_screencast)     │   TCP      │  MediaCodec H.264   │
│        ▼                                │ ─────────▶ │  low-latency decode │
│ cudaconvert → NVENC H.264 ──────────────┼─ video ──▶ │  → panel surface    │
│                                         │            │                     │
│ org_kde_kwin_fake_input  ◀──────────────┼─ input ─── │ pointer/keys/text   │
└────────────────────────────────────────┘            └─────────────────────┘
          mDNS: _qwayland._tcp  (headset finds the PC automatically)
```

## Status

Early but working. Measured on a Quest 3 over Wi‑Fi (5 GHz) with an NVIDIA RTX
GPU: 2560×1440 per display, ~45 fps for an active desktop with every frame
decoded, 5–15 ms network round trip. Two simultaneous displays tested.

Known gaps:

- Only KDE Plasma 6 (KWin) is supported; GNOME/wlroots would need a different
  virtual-output backend.
- Encoding uses NVENC when available and falls back to x264 (CPU) otherwise.
  VAAPI (Intel/AMD) is not wired up yet.
- The panel is a regular 2D Horizon OS panel. A direct-to-compositor OpenXR
  layer would give sharper text; not done yet.
- No authentication or encryption: run it on a trusted network only.

## Requirements

PC: KDE Plasma 6 on Wayland, PipeWire, GStreamer ≥ 1.22 with
`pipewiresrc`, `nvh264enc` and the CUDA elements (`gstreamer1-plugins-bad`),
Python 3 with PyGObject, `avahi-publish-service`, `kscreen-doctor`.
To build: a C compiler, `wayland-devel`, `plasma-wayland-protocols-devel`
(Fedora package names).

Headset: developer mode enabled, so the app can be sideloaded with `adb`.
To build the APK: the Android SDK (platform 34, build-tools) and a JDK ≥ 11.
No Gradle needed.

## Install

```sh
# PC
make install                       # into ~/.local, no root needed
systemctl --user enable --now qwayland

# Headset (USB or wireless adb)
make install-apk
```

`make install` also installs `~/.local/share/applications/qwayland-vout.desktop`.
KWin only lets whitelisted executables create virtual outputs and inject input,
and this file is that whitelist entry.

Then open **qwayland** from the headset's app library (under *Unknown
sources*). It finds the PC over mDNS and a new monitor appears. Use the
**+ display** button for more monitors and **keyboard** for the system keyboard.
A Bluetooth keyboard or mouse paired with the headset also works.

## Configuration

```
qwayland-server [--port 7710] [--width 2560 --height 1440] [--scale 1.0]
                [--fps 60] [--bitrate 40000] [--name NAME] [-v]
```

The panel can request its own size and scale. These can be set per panel with
`adb shell am start -n org.qwayland.client/.DisplayActivity --ei width 1920
--ei height 1080`.

## How it works

* `server/vout/qw-vout.c`: a small Wayland client that uses KWin's privileged
  `zkde_screencast_unstable_v1.stream_virtual_output` to create monitors
  (KWin hands back a PipeWire node per monitor) and
  `org_kde_kwin_fake_input` to inject pointer, keys and keysyms.
* `server/qwayland_server.py`: asyncio server. It handles discovery, one
  GStreamer pipeline per panel (`pipewiresrc → cudaconvert → nvh264enc →
  appsink`), frame dropping with keyframe resync when the network can't keep up,
  and explicit KScreen layout so displays line up right of your real monitors.
  The wire protocol is documented at the top of the file.
* `client/`: plain Java Android app (no dependencies). Each activity instance
  is one panel and one display.

## Native "Virtual Display" support?

Horizon OS has a built-in *Virtual Display* feature for Mac and Windows. We
investigated making Linux speak it directly; see
[docs/native-virtual-display.md](docs/native-virtual-display.md). The short
version: Windows support lives inside Microsoft's own app, and Meta's LAN
protocol is gated behind a server-side feature flag and Meta-signed device
certificates. qwayland uses its own panel app instead.

## License

MIT, see [LICENSE](LICENSE).
