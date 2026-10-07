package org.qwayland.client;

import android.util.SparseIntArray;
import android.view.KeyEvent;

/**
 * Maps Android key events to Linux evdev key codes (linux/input-event-codes.h).
 * Hardware keyboards already report the evdev code as the scan code; this
 * table covers virtual keyboards, which only provide Android key codes.
 */
final class KeyMap {
    private static final SparseIntArray MAP = new SparseIntArray();

    private static void map(int android, int evdev) {
        MAP.put(android, evdev);
    }

    static {
        int[] letters = {30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38, 50, 49, 24, 25, 16, 19, 31, 20,
                22, 47, 17, 45, 21, 44}; // a..z
        for (int i = 0; i < 26; i++) map(KeyEvent.KEYCODE_A + i, letters[i]);
        map(KeyEvent.KEYCODE_0, 11);
        for (int i = 1; i <= 9; i++) map(KeyEvent.KEYCODE_0 + i, 1 + i); // 1..9 -> 2..10
        int[] fkeys = {59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88};
        for (int i = 0; i < 12; i++) map(KeyEvent.KEYCODE_F1 + i, fkeys[i]);

        map(KeyEvent.KEYCODE_ESCAPE, 1);
        map(KeyEvent.KEYCODE_MINUS, 12);
        map(KeyEvent.KEYCODE_EQUALS, 13);
        map(KeyEvent.KEYCODE_DEL, 14); // backspace
        map(KeyEvent.KEYCODE_TAB, 15);
        map(KeyEvent.KEYCODE_LEFT_BRACKET, 26);
        map(KeyEvent.KEYCODE_RIGHT_BRACKET, 27);
        map(KeyEvent.KEYCODE_ENTER, 28);
        map(KeyEvent.KEYCODE_CTRL_LEFT, 29);
        map(KeyEvent.KEYCODE_SEMICOLON, 39);
        map(KeyEvent.KEYCODE_APOSTROPHE, 40);
        map(KeyEvent.KEYCODE_GRAVE, 41);
        map(KeyEvent.KEYCODE_SHIFT_LEFT, 42);
        map(KeyEvent.KEYCODE_BACKSLASH, 43);
        map(KeyEvent.KEYCODE_COMMA, 51);
        map(KeyEvent.KEYCODE_PERIOD, 52);
        map(KeyEvent.KEYCODE_SLASH, 53);
        map(KeyEvent.KEYCODE_SHIFT_RIGHT, 54);
        map(KeyEvent.KEYCODE_ALT_LEFT, 56);
        map(KeyEvent.KEYCODE_SPACE, 57);
        map(KeyEvent.KEYCODE_CAPS_LOCK, 58);
        map(KeyEvent.KEYCODE_CTRL_RIGHT, 97);
        map(KeyEvent.KEYCODE_ALT_RIGHT, 100);
        map(KeyEvent.KEYCODE_MOVE_HOME, 102);
        map(KeyEvent.KEYCODE_DPAD_UP, 103);
        map(KeyEvent.KEYCODE_PAGE_UP, 104);
        map(KeyEvent.KEYCODE_DPAD_LEFT, 105);
        map(KeyEvent.KEYCODE_DPAD_RIGHT, 106);
        map(KeyEvent.KEYCODE_MOVE_END, 107);
        map(KeyEvent.KEYCODE_DPAD_DOWN, 108);
        map(KeyEvent.KEYCODE_PAGE_DOWN, 109);
        map(KeyEvent.KEYCODE_INSERT, 110);
        map(KeyEvent.KEYCODE_FORWARD_DEL, 111);
        map(KeyEvent.KEYCODE_META_LEFT, 125);
        map(KeyEvent.KEYCODE_META_RIGHT, 126);
        map(KeyEvent.KEYCODE_MENU, 127);
    }

    static final int KEY_LEFTSHIFT = 42;

    /** Returns the evdev code for an event, or 0 if it has none. */
    static int evdev(KeyEvent e) {
        int scan = e.getScanCode();
        if (scan > 0 && e.getDevice() != null && !e.getDevice().isVirtual()) return scan;
        return MAP.get(e.getKeyCode(), 0);
    }

    private KeyMap() {
    }
}
