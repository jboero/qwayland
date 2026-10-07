# Native Horizon OS "Virtual Display" and Linux

Horizon OS ships a built-in *Virtual Display* app (`com.oculus.remotedesktop`)
that turns a Mac or Windows PC into floating monitors. These notes record what
we learned while checking whether a Linux machine could act as the PC side
directly, without a custom headset app. The investigation was for
interoperability and used a headset we own: we looked at the app installed on
it (v177, October 2026), its logs and its configuration. No Meta or Microsoft
code is included in this repository.

## Summary

The headset app contains three separate ways to reach a computer. None of them
can be used by Linux today.

| Path | Used for | Discovery | Trust / gating | Linux feasibility |
|------|----------|-----------|----------------|-------------------|
| Microsoft Windows App | Windows 11 + "Mixed Reality Link" | Bluetooth LE + QR code (`mr-link:` URI) | Entirely inside Microsoft's Windows App and PC app | Needs reverse engineering two Microsoft binaries; very large |
| MRDS / xr2ds | Likely the Mac app and older Windows app (unconfirmed) | Meta cloud brokers (MQTT / "DGW"); no local discovery | Meta account on both ends, cloud signaling, WebRTC media | Would mean impersonating Meta's desktop app against Meta's servers; not pursued |
| Highwind / DISCO | Newer direct LAN and USB transport | mDNS on the LAN | Server-side feature flag, off by default; Meta-signed device certificates | Promising, but blocked by the flag (see below) |

## Highwind (the LAN protocol)

What the headset does when the feature is enabled:

* **Discovery.** It browses mDNS for `_highwind_sp_v1._tcp`. A server must
  publish TXT keys `tt` (transport type), `etn` / `etv` (endpoint type name
  and version), `ein` / `eiu` (endpoint name and UUID). An optional `ipo`
  key overrides the address. Host and port come from the SRV record. The
  exact values a real Mac or Windows server publishes have not been observed
  yet.
* **Transport.** Plain TCP to the advertised port, upgraded to a WebSocket.
  Inside it, numbered "topics" are multiplexed: control, state exchange,
  video, audio and input. There is a two-phase welcome/acknowledge handshake
  with protocol version negotiation. Messages use a Meta-internal versioned
  binary serialization, not protobuf.
* **Trust.** Each endpoint has an Ed25519 device certificate that is signed by
  Meta's servers for the user's account. On connect, the two sides exchange
  certificates and prove they hold the keys. The headset checks the peer's
  signature against Meta public keys built into the app. If both devices
  belong to the same account, pairing is silent. Otherwise the code falls back
  to a confirmation step with a pairing code. **Untested:** whether that
  fallback accepts a certificate that Meta has not signed. If it does, a
  third-party server is possible. If it doesn't, it isn't.
* **Media.** The server encodes H.264 or HEVC, sent as slices on a video
  topic with a dynamic bitrate controller. Audio is Opus. Display count and
  resolution are negotiated in a stream-configuration message.
* **Operating system.** The server tells the headset its OS after
  connecting. Feature flags exist only for macOS and Windows, so a Linux
  server would have to report one of those.

### The blocker: a server-side flag

The headset only starts Highwind when the device-config flag
`oculus_remote_desktop:disco_wireless_enabled` (or the USB equivalent) is
true. The app has no default for it, so the flag is false unless Meta's
config service delivers it. Meta's logs say *"both DISCO transports disabled,
not starting Crosswind"* in that case. On a retail (`user`) build, ADB cannot
change it:

* the config service's override receiver is not exported,
* the system property that selects the config service is protected, and
* both apps disable `adb backup`.

So whether Highwind runs on a given headset is up to Meta's rollout. If your
headset is signed in and the flag is on, `adb logcat | grep -i crosswind`
shows *"maybeStartCrosswind, usb [..], wireless [true]"*.

## If someone wants to continue

1. On a headset where the flag is on, publish a fake `_highwind_sp_v1._tcp`
   service (`tools/highwind-probe.sh`) and watch `adb logcat` for the
   `HIGHWIND_CLIENT` and `com.oculus.highwindservice.ServiceDiscovery` tags.
   That confirms discovery and captures the first bytes of the handshake.
2. Settle the trust question: does manual pairing-code confirmation accept a
   self-signed server certificate? Everything else is ordinary protocol
   work.
3. Capture `avahi-browse -r _highwind_sp_v1._tcp` next to a real Mac running
   Meta's *Virtual Display* app to learn the real TXT values.

Until then, qwayland's own panel app is the practical route.
