package org.qwayland.client;

import android.app.Activity;
import android.content.Context;
import android.text.InputType;
import android.view.KeyEvent;
import android.view.View;
import android.view.inputmethod.BaseInputConnection;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputConnection;

/**
 * Invisible text target for the headset's system keyboard. Committed text is
 * forwarded as characters (the server types them as keysyms); deletions and
 * editor actions become key presses.
 */
final class KeyboardSink extends View {
    interface Target {
        void onText(String text);

        void onKey(int evdevCode);
    }

    private static final int KEY_BACKSPACE = 14, KEY_ENTER = 28;
    private final Target target;

    KeyboardSink(Context context, Target target) {
        super(context);
        this.target = target;
        setFocusable(true);
        setFocusableInTouchMode(true);
    }

    @Override
    public boolean onCheckIsTextEditor() {
        return true;
    }

    @Override
    public InputConnection onCreateInputConnection(EditorInfo out) {
        // Visible-password disables prediction/composition so every keystroke
        // is committed immediately.
        out.inputType = InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD;
        out.imeOptions = EditorInfo.IME_FLAG_NO_EXTRACT_UI | EditorInfo.IME_FLAG_NO_FULLSCREEN
                | EditorInfo.IME_ACTION_NONE;
        return new BaseInputConnection(this, false) {
            @Override
            public boolean commitText(CharSequence text, int newCursorPosition) {
                if (text.length() > 0) target.onText(text.toString());
                return true;
            }

            @Override
            public boolean setComposingText(CharSequence text, int newCursorPosition) {
                return commitText(text, newCursorPosition);
            }

            @Override
            public boolean deleteSurroundingText(int before, int after) {
                for (int i = 0; i < before; i++) target.onKey(KEY_BACKSPACE);
                return true;
            }

            @Override
            public boolean performEditorAction(int action) {
                target.onKey(KEY_ENTER);
                return true;
            }

            @Override
            public boolean sendKeyEvent(KeyEvent e) {
                // Route through the activity so it reaches the evdev key mapping.
                return ((Activity) getContext()).dispatchKeyEvent(e);
            }
        };
    }
}
