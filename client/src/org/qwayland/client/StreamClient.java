package org.qwayland.client;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Handler;
import android.os.HandlerThread;
import android.util.Log;
import android.view.Surface;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.DataInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;

/**
 * Connects to a qwayland server, decodes its H.264 stream onto a Surface and
 * sends input back. See server/qwayland_server.py for the wire protocol.
 */
final class StreamClient {
    private static final String TAG = "qwayland";

    static final int MSG_CONFIG = 0x01, MSG_VIDEO = 0x02, MSG_PONG = 0x03, MSG_ERROR = 0x04;
    static final int MSG_HELLO = 0x10, MSG_POINTER = 0x11, MSG_BUTTON = 0x12, MSG_SCROLL = 0x13,
            MSG_KEY = 0x14, MSG_KEYFRAME = 0x15, MSG_PING = 0x16, MSG_TEXT = 0x17;

    interface Listener {
        void onStatus(String status);
        void onConfigured(int width, int height, String name);
        void onFirstFrame();
        void onClosed(String reason);
    }

    private final String host;
    private final int port;
    private final int width;
    private final int height;
    private final Surface surface;
    private final Listener listener;

    private volatile boolean running = true;
    private Socket socket;
    private OutputStream out;
    private MediaCodec codec;
    private Thread readThread;
    private Thread drainThread;

    // Stats, logged every few seconds and on request (adb logcat -s qwayland).
    private long statsStart;
    private int framesIn, framesOut;
    private long bytesIn;
    private volatile long lastRttUs = -1;

    // All socket writes happen on this thread (Android forbids network I/O on
    // the UI thread). Pointer motion is coalesced to the latest position.
    private final HandlerThread sendThread = new HandlerThread("qwayland-send");
    private Handler sender;
    private final Object pointerLock = new Object();
    private boolean pointerPending;
    private float pendingX, pendingY;

    StreamClient(String host, int port, int width, int height, Surface surface, Listener listener) {
        this.host = host;
        this.port = port;
        this.width = width;
        this.height = height;
        this.surface = surface;
        this.listener = listener;
    }

    void start() {
        sendThread.start();
        sender = new Handler(sendThread.getLooper());
        readThread = new Thread(this::run, "qwayland-net");
        readThread.start();
    }

    void stop() {
        running = false;
        sendThread.quitSafely();
        try {
            if (socket != null) socket.close();
        } catch (IOException ignored) {
        }
    }

    // ---- input (called from the UI thread) ----

    void sendPointer(float nx, float ny) {
        synchronized (pointerLock) {
            pendingX = nx;
            pendingY = ny;
            if (pointerPending) return;
            pointerPending = true;
        }
        post(() -> {
            float x, y;
            synchronized (pointerLock) {
                x = pendingX;
                y = pendingY;
                pointerPending = false;
            }
            ByteBuffer b = payload(8);
            b.putFloat(x).putFloat(y);
            write(MSG_POINTER, b.array());
        });
    }

    void sendButton(int evdevCode, boolean pressed) {
        ByteBuffer b = payload(5);
        b.putInt(evdevCode).put((byte) (pressed ? 1 : 0));
        send(MSG_BUTTON, b);
    }

    void sendScroll(int axis, float value) {
        ByteBuffer b = payload(5);
        b.put((byte) axis).putFloat(value);
        send(MSG_SCROLL, b);
    }

    void sendKey(int evdevCode, boolean pressed) {
        ByteBuffer b = payload(5);
        b.putInt(evdevCode).put((byte) (pressed ? 1 : 0));
        send(MSG_KEY, b);
    }

    void sendText(String text) {
        send(MSG_TEXT, text.getBytes(StandardCharsets.UTF_8));
    }

    void tapKey(int evdevCode) {
        sendKey(evdevCode, true);
        sendKey(evdevCode, false);
    }

    private static ByteBuffer payload(int size) {
        return ByteBuffer.allocate(size).order(ByteOrder.LITTLE_ENDIAN);
    }

    private void send(int type, ByteBuffer payload) {
        send(type, payload.array());
    }

    private void send(int type, byte[] payload) {
        post(() -> write(type, payload));
    }

    private void post(Runnable r) {
        Handler h = sender;
        if (h != null) h.post(r);
    }

    private void write(int type, byte[] payload) {
        OutputStream o = out;
        if (o == null) return;
        ByteBuffer msg = payload(5 + payload.length);
        msg.put((byte) type).putInt(payload.length).put(payload);
        try {
            o.write(msg.array());
        } catch (IOException e) {
            Log.w(TAG, "send failed", e);
        }
    }

    // ---- network / decode ----

    private void run() {
        String reason = "disconnected";
        try {
            listener.onStatus("Connecting to " + host + ":" + port + "…");
            socket = new Socket();
            socket.setTcpNoDelay(true);
            socket.setReceiveBufferSize(4 * 1024 * 1024);
            socket.connect(new InetSocketAddress(host, port), 5000);
            out = socket.getOutputStream();
            DataInputStream in = new DataInputStream(
                    new BufferedInputStream(socket.getInputStream(), 1 << 20));

            JSONObject hello = new JSONObject();
            hello.put("proto", 1);
            hello.put("width", width);
            hello.put("height", height);
            send(MSG_HELLO, hello.toString().getBytes(StandardCharsets.UTF_8));
            listener.onStatus("Waiting for display…");

            byte[] header = new byte[5];
            boolean firstFrame = true;
            while (running) {
                in.readFully(header);
                ByteBuffer h = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN);
                int type = h.get() & 0xff;
                int len = h.getInt();
                if (len < 0 || len > 64 * 1024 * 1024) throw new IOException("bad length " + len);
                byte[] body = new byte[len];
                in.readFully(body);

                if (type == MSG_CONFIG) {
                    JSONObject cfg = new JSONObject(new String(body, StandardCharsets.UTF_8));
                    int w = cfg.getInt("width"), hgt = cfg.getInt("height");
                    startCodec(w, hgt);
                    listener.onConfigured(w, hgt, cfg.optString("name", host));
                } else if (type == MSG_VIDEO && codec != null) {
                    ByteBuffer v = ByteBuffer.wrap(body).order(ByteOrder.LITTLE_ENDIAN);
                    long ptsUs = v.getLong();
                    int flags = v.get();
                    queueFrame(body, 9, len - 9, ptsUs, (flags & 1) != 0);
                    framesIn++;
                    bytesIn += len;
                    maybeLogStats();
                    if (firstFrame) {
                        firstFrame = false;
                        listener.onFirstFrame();
                    }
                } else if (type == MSG_PONG) {
                    long sent = ByteBuffer.wrap(body).order(ByteOrder.LITTLE_ENDIAN).getLong();
                    lastRttUs = System.nanoTime() / 1000 - sent;
                } else if (type == MSG_ERROR) {
                    reason = "Server error: " + new String(body, StandardCharsets.UTF_8);
                    break;
                }
            }
        } catch (Exception e) {
            if (running) {
                Log.w(TAG, "stream ended", e);
                reason = e.getClass().getSimpleName() + ": " + e.getMessage();
            }
        } finally {
            running = false;
            stopCodec();
            try {
                if (socket != null) socket.close();
            } catch (IOException ignored) {
            }
            listener.onClosed(reason);
        }
    }

    private void maybeLogStats() {
        long now = System.nanoTime();
        if (statsStart == 0) statsStart = now;
        double secs = (now - statsStart) / 1e9;
        if (secs < 5) return;
        Log.i(TAG, String.format("stats: rx %.1f fps, decoded %.1f fps, %.1f Mbit/s, rtt %s",
                framesIn / secs, framesOut / secs, bytesIn * 8 / secs / 1e6,
                lastRttUs < 0 ? "?" : (lastRttUs / 1000.0) + " ms"));
        statsStart = now;
        framesIn = framesOut = 0;
        bytesIn = 0;
        ByteBuffer ping = payload(8);
        ping.putLong(now / 1000);
        send(MSG_PING, ping);
    }

    private void startCodec(int w, int h) throws IOException {
        stopCodec();
        MediaFormat fmt = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, w, h);
        fmt.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
        fmt.setInteger(MediaFormat.KEY_PRIORITY, 0); // realtime
        fmt.setInteger(MediaFormat.KEY_OPERATING_RATE, Short.MAX_VALUE);
        fmt.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, w * h);
        codec = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC);
        codec.configure(fmt, surface, null, 0);
        codec.start();
        final MediaCodec c = codec;
        drainThread = new Thread(() -> drain(c), "qwayland-decode");
        drainThread.start();
    }

    private void queueFrame(byte[] data, int off, int len, long ptsUs, boolean keyframe) {
        MediaCodec c = codec;
        try {
            int idx = c.dequeueInputBuffer(100_000);
            if (idx < 0) {
                // Decoder is backed up; drop this frame and resync on a keyframe.
                send(MSG_KEYFRAME, new byte[0]);
                return;
            }
            ByteBuffer buf = c.getInputBuffer(idx);
            buf.clear();
            buf.put(data, off, len);
            c.queueInputBuffer(idx, 0, len, ptsUs, keyframe ? MediaCodec.BUFFER_FLAG_KEY_FRAME : 0);
        } catch (IllegalStateException e) {
            Log.w(TAG, "queue failed", e);
        }
    }

    private void drain(MediaCodec c) {
        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        while (running && c == codec) {
            try {
                int idx = c.dequeueOutputBuffer(info, 20_000);
                if (idx >= 0) {
                    c.releaseOutputBuffer(idx, true);
                    framesOut++;
                }
            } catch (IllegalStateException e) {
                break;
            }
        }
    }

    private void stopCodec() {
        MediaCodec c = codec;
        codec = null;
        if (c == null) return;
        try {
            if (drainThread != null) drainThread.join(200);
        } catch (InterruptedException ignored) {
        }
        try {
            c.stop();
        } catch (Exception ignored) {
        }
        c.release();
    }
}
