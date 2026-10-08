package org.qwayland.client;

import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;

/** Which video codecs this headset can decode in hardware, and with which decoder. */
final class Decoders {
    private static final String TAG = "qwayland";
    // Protocol codec names, in our order of preference.
    static final String[] CODECS = {"av1", "h265", "h264"};

    static String mimeFor(String codec) {
        switch (codec) {
            case "av1":
                return MediaFormat.MIMETYPE_VIDEO_AV1;
            case "h265":
                return MediaFormat.MIMETYPE_VIDEO_HEVC;
            default:
                return MediaFormat.MIMETYPE_VIDEO_AVC;
        }
    }

    /** Codecs with a hardware decoder, advertised to the server in HELLO. */
    static List<String> supported() {
        List<String> out = new ArrayList<>();
        for (String codec : CODECS) {
            if (bestDecoder(mimeFor(codec)) != null) out.add(codec);
        }
        if (out.isEmpty()) out.add("h264"); // let the platform pick something
        return out;
    }

    /**
     * Picks a hardware decoder for {@code mime}, preferring one that supports
     * low-latency mode (Qualcomm ships dedicated "*.low_latency" variants).
     */
    static String bestDecoder(String mime) {
        MediaCodecInfo best = null;
        int bestScore = -1;
        for (MediaCodecInfo info : new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()) {
            if (info.isEncoder() || !info.isHardwareAccelerated() || info.getName().contains(".secure")) {
                continue;
            }
            for (String type : info.getSupportedTypes()) {
                if (!type.equalsIgnoreCase(mime)) continue;
                int score = 1;
                MediaCodecInfo.CodecCapabilities caps = info.getCapabilitiesForType(type);
                if (caps.isFeatureSupported(MediaCodecInfo.CodecCapabilities.FEATURE_LowLatency)) score += 2;
                if (info.getName().contains("low_latency")) score += 1;
                if (score > bestScore) {
                    best = info;
                    bestScore = score;
                }
            }
        }
        if (best != null) Log.i(TAG, "decoder for " + mime + ": " + best.getName());
        return best == null ? null : best.getName();
    }

    private Decoders() {
    }
}
