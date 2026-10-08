package org.qwayland.client;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.InputDevice;
import android.view.KeyEvent;
import android.view.MotionEvent;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * One panel = one virtual display on the Linux desktop. Launching the activity
 * again (the "+" button) opens another panel and another display.
 */
public class DisplayActivity extends Activity implements StreamClient.Listener, SurfaceHolder.Callback {
    private static final String TAG = "qwayland";
    private static final String SERVICE_TYPE = "_qwayland._tcp";
    static final String EXTRA_HOST = "host";
    static final String EXTRA_PORT = "port";
    static final String EXTRA_WIDTH = "width";
    static final String EXTRA_HEIGHT = "height";
    static final String EXTRA_PANEL = "panel";

    private static final int BTN_LEFT = 0x110, BTN_RIGHT = 0x111, BTN_MIDDLE = 0x112;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private FrameLayout root;
    private SurfaceView surfaceView;
    private TextView status;
    private Button addButton;
    private LinearLayout toolbar;
    private KeyboardSink keyboardSink;

    private StreamClient client;
    private String host;
    private int port = 7710;
    private int reqWidth, reqHeight;
    private String panelId;
    private int streamWidth, streamHeight;
    private boolean surfaceReady;
    private boolean visible;
    private int pressedButton;

    private NsdManager nsd;
    private NsdManager.DiscoveryListener discovery;
    private WifiManager.MulticastLock multicastLock;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent i = getIntent();
        host = i.getStringExtra(EXTRA_HOST);
        port = i.getIntExtra(EXTRA_PORT, port);
        reqWidth = i.getIntExtra(EXTRA_WIDTH, 2560);
        reqHeight = i.getIntExtra(EXTRA_HEIGHT, 1440);
        // The first panel is "main"; extra panels get a random id that
        // survives activity recreation, so each keeps its display slot.
        panelId = savedInstanceState != null ? savedInstanceState.getString(EXTRA_PANEL) : null;
        if (panelId == null) panelId = i.getStringExtra(EXTRA_PANEL);
        if (panelId == null) panelId = "main";

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        surfaceView = new SurfaceView(this);
        surfaceView.getHolder().addCallback(this);
        root.addView(surfaceView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        status = new TextView(this);
        status.setTextColor(Color.WHITE);
        status.setTextSize(22);
        status.setGravity(Gravity.CENTER);
        root.addView(status, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT, Gravity.CENTER));

        keyboardSink = new KeyboardSink(this, new KeyboardSink.Target() {
            @Override
            public void onText(String text) {
                if (client != null) client.sendText(text);
            }

            @Override
            public void onKey(int evdevCode) {
                if (client != null) client.tapKey(evdevCode);
            }
        });
        root.addView(keyboardSink, new FrameLayout.LayoutParams(1, 1));

        toolbar = new LinearLayout(this);
        toolbar.setAlpha(0.85f);
        Button keyboardButton = new Button(this);
        keyboardButton.setText("keyboard");
        keyboardButton.setOnClickListener(v -> toggleKeyboard());
        addButton = new Button(this);
        addButton.setText("+ display");
        addButton.setOnClickListener(v -> openAnotherDisplay());
        toolbar.addView(keyboardButton);
        toolbar.addView(addButton);
        root.addView(toolbar, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.TOP | Gravity.END));

        root.addOnLayoutChangeListener((v, l, t, r, b, ol, ot, or, ob) -> fitSurface());
        setContentView(root);
        installInputHandlers();
        setStatus("Looking for a qwayland server…");
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        out.putString(EXTRA_PANEL, panelId);
    }

    // Connect while the panel is visible, not just while it is focused:
    // Horizon OS pauses panels whenever focus moves elsewhere (system menu,
    // another panel), and dropping the connection then would destroy the
    // virtual display and throw its windows back onto the other monitors.
    @Override
    protected void onStart() {
        super.onStart();
        visible = true;
        maybeConnect();
    }

    @Override
    protected void onStop() {
        super.onStop();
        visible = false;
        disconnect();
        stopDiscovery();
    }

    // ---- surface ----

    @Override
    public void surfaceCreated(SurfaceHolder holder) {
        surfaceReady = true;
        maybeConnect();
    }

    @Override
    public void surfaceChanged(SurfaceHolder holder, int format, int width, int height) {
    }

    @Override
    public void surfaceDestroyed(SurfaceHolder holder) {
        surfaceReady = false;
        disconnect();
    }

    /** Letterbox the surface to the stream's aspect ratio. */
    private void fitSurface() {
        if (streamWidth == 0 || root.getWidth() == 0) return;
        float scale = Math.min(root.getWidth() / (float) streamWidth, root.getHeight() / (float) streamHeight);
        int w = Math.round(streamWidth * scale), h = Math.round(streamHeight * scale);
        FrameLayout.LayoutParams lp = (FrameLayout.LayoutParams) surfaceView.getLayoutParams();
        if (lp.width != w || lp.height != h) {
            lp.width = w;
            lp.height = h;
            surfaceView.setLayoutParams(lp);
        }
    }

    // ---- connection ----

    private void maybeConnect() {
        if (!visible || !surfaceReady || client != null) return;
        if (host == null) {
            startDiscovery();
            return;
        }
        client = new StreamClient(host, port, reqWidth, reqHeight, panelId,
                surfaceView.getHolder().getSurface(), this);
        client.start();
    }

    private void disconnect() {
        if (client != null) {
            client.stop();
            client = null;
        }
    }

    private void toggleKeyboard() {
        InputMethodManager imm = (InputMethodManager) getSystemService(Context.INPUT_METHOD_SERVICE);
        if (keyboardSink.hasFocus() && imm.isActive(keyboardSink)) {
            imm.hideSoftInputFromWindow(keyboardSink.getWindowToken(), 0);
            keyboardSink.clearFocus();
        } else {
            keyboardSink.requestFocus();
            imm.showSoftInput(keyboardSink, InputMethodManager.SHOW_IMPLICIT);
        }
    }

    private void openAnotherDisplay() {
        if (host == null) return;
        Intent i = new Intent(this, DisplayActivity.class);
        i.putExtra(EXTRA_HOST, host);
        i.putExtra(EXTRA_PORT, port);
        i.putExtra(EXTRA_WIDTH, reqWidth);
        i.putExtra(EXTRA_HEIGHT, reqHeight);
        i.putExtra(EXTRA_PANEL, java.util.UUID.randomUUID().toString());
        i.addFlags(Intent.FLAG_ACTIVITY_NEW_DOCUMENT | Intent.FLAG_ACTIVITY_MULTIPLE_TASK);
        startActivity(i);
    }

    private void startDiscovery() {
        if (discovery != null) return;
        WifiManager wifi = (WifiManager) getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        multicastLock = wifi.createMulticastLock("qwayland");
        multicastLock.acquire();
        nsd = (NsdManager) getSystemService(Context.NSD_SERVICE);
        discovery = new NsdManager.DiscoveryListener() {
            @Override
            public void onServiceFound(NsdServiceInfo info) {
                Log.i(TAG, "found " + info);
                nsd.resolveService(info, new NsdManager.ResolveListener() {
                    @Override
                    public void onResolveFailed(NsdServiceInfo i, int err) {
                        Log.w(TAG, "resolve failed " + err);
                    }

                    @Override
                    public void onServiceResolved(NsdServiceInfo i) {
                        ui.post(() -> onServerFound(i.getHost().getHostAddress(), i.getPort()));
                    }
                });
            }

            @Override
            public void onServiceLost(NsdServiceInfo info) {
            }

            @Override
            public void onDiscoveryStarted(String type) {
            }

            @Override
            public void onDiscoveryStopped(String type) {
            }

            @Override
            public void onStartDiscoveryFailed(String type, int err) {
                setStatus("mDNS discovery failed (" + err + ")");
            }

            @Override
            public void onStopDiscoveryFailed(String type, int err) {
            }
        };
        nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discovery);

        // Fall back to the last server we used if discovery stays quiet.
        SharedPreferences prefs = getPreferences(MODE_PRIVATE);
        String last = prefs.getString("host", null);
        if (last != null) {
            int lastPort = prefs.getInt("port", port);
            ui.postDelayed(() -> {
                if (host == null) onServerFound(last, lastPort);
            }, 4000);
        }
    }

    private void stopDiscovery() {
        if (discovery != null) {
            try {
                nsd.stopServiceDiscovery(discovery);
            } catch (IllegalArgumentException ignored) {
            }
            discovery = null;
        }
        if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
    }

    private void onServerFound(String address, int servicePort) {
        if (host != null) return;
        host = address;
        port = servicePort;
        getPreferences(MODE_PRIVATE).edit().putString("host", host).putInt("port", port).apply();
        stopDiscovery();
        maybeConnect();
    }

    // ---- StreamClient.Listener (network thread) ----

    @Override
    public void onStatus(String s) {
        setStatus(s);
    }

    @Override
    public void onConfigured(int width, int height, String name) {
        ui.post(() -> {
            streamWidth = width;
            streamHeight = height;
            surfaceView.getHolder().setFixedSize(width, height);
            fitSurface();
            setTitle(name);
        });
    }

    @Override
    public void onFirstFrame() {
        ui.post(() -> {
            status.setVisibility(View.GONE);
            toolbar.setAlpha(0.35f);
        });
    }

    @Override
    public void onClosed(String reason) {
        ui.post(() -> {
            client = null;
            setStatus(reason + "\nReconnecting…");
            toolbar.setAlpha(0.85f);
            ui.postDelayed(this::maybeConnect, 2000);
        });
    }

    private void setStatus(String s) {
        ui.post(() -> {
            status.setText(s);
            status.setVisibility(View.VISIBLE);
        });
    }

    // ---- input ----

    private void installInputHandlers() {
        surfaceView.setOnHoverListener((v, e) -> {
            sendPointer(e);
            return true;
        });
        surfaceView.setOnTouchListener((v, e) -> {
            StreamClient c = client;
            if (c == null) return true;
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    sendPointer(e);
                    pressedButton = buttonFor(e);
                    c.sendButton(pressedButton, true);
                    break;
                case MotionEvent.ACTION_MOVE:
                    sendPointer(e);
                    break;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    sendPointer(e);
                    if (pressedButton != 0) c.sendButton(pressedButton, false);
                    pressedButton = 0;
                    break;
            }
            return true;
        });
        surfaceView.setOnGenericMotionListener((v, e) -> {
            StreamClient c = client;
            if (c == null) return false;
            switch (e.getActionMasked()) {
                case MotionEvent.ACTION_SCROLL:
                    float vs = e.getAxisValue(MotionEvent.AXIS_VSCROLL);
                    float hs = e.getAxisValue(MotionEvent.AXIS_HSCROLL);
                    if (vs != 0) c.sendScroll(0, -vs * 15f);
                    if (hs != 0) c.sendScroll(1, hs * 15f);
                    return true;
                case MotionEvent.ACTION_BUTTON_PRESS:
                case MotionEvent.ACTION_BUTTON_RELEASE:
                    int b = mapButton(e.getActionButton());
                    if (b != 0) c.sendButton(b, e.getActionMasked() == MotionEvent.ACTION_BUTTON_PRESS);
                    return true;
                case MotionEvent.ACTION_HOVER_MOVE:
                    sendPointer(e);
                    return true;
            }
            return false;
        });
    }

    private int buttonFor(MotionEvent e) {
        if (e.isFromSource(InputDevice.SOURCE_MOUSE)) {
            int state = e.getButtonState();
            if ((state & MotionEvent.BUTTON_SECONDARY) != 0) return BTN_RIGHT;
            if ((state & MotionEvent.BUTTON_TERTIARY) != 0) return BTN_MIDDLE;
        }
        return BTN_LEFT;
    }

    private static int mapButton(int androidButton) {
        switch (androidButton) {
            case MotionEvent.BUTTON_PRIMARY:
                return BTN_LEFT;
            case MotionEvent.BUTTON_SECONDARY:
                return BTN_RIGHT;
            case MotionEvent.BUTTON_TERTIARY:
                return BTN_MIDDLE;
            default:
                return 0;
        }
    }

    private void sendPointer(MotionEvent e) {
        StreamClient c = client;
        if (c == null || surfaceView.getWidth() == 0) return;
        c.sendPointer(e.getX() / surfaceView.getWidth(), e.getY() / surfaceView.getHeight());
    }

    @Override
    public boolean dispatchKeyEvent(KeyEvent e) {
        StreamClient c = client;
        int code = KeyMap.evdev(e);
        if (c == null || code == 0 || e.getAction() == KeyEvent.ACTION_MULTIPLE) {
            return super.dispatchKeyEvent(e);
        }
        if (e.getRepeatCount() == 0) c.sendKey(code, e.getAction() == KeyEvent.ACTION_DOWN);
        return true;
    }
}
