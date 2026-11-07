package io.github.orimovo.curslink

import android.view.KeyEvent

/**
 * Curslink key names (curslink/inputs/keys.py: a character, or a pynput key
 * name such as "enter", "shift_l", "f5") as Android key codes.
 */
object Keys {
    private val special = mapOf(
        "enter" to KeyEvent.KEYCODE_ENTER,
        "backspace" to KeyEvent.KEYCODE_DEL,
        "delete" to KeyEvent.KEYCODE_FORWARD_DEL,
        "tab" to KeyEvent.KEYCODE_TAB,
        "space" to KeyEvent.KEYCODE_SPACE,
        "esc" to KeyEvent.KEYCODE_ESCAPE,
        "up" to KeyEvent.KEYCODE_DPAD_UP,
        "down" to KeyEvent.KEYCODE_DPAD_DOWN,
        "left" to KeyEvent.KEYCODE_DPAD_LEFT,
        "right" to KeyEvent.KEYCODE_DPAD_RIGHT,
        "home" to KeyEvent.KEYCODE_MOVE_HOME,
        "end" to KeyEvent.KEYCODE_MOVE_END,
        "page_up" to KeyEvent.KEYCODE_PAGE_UP,
        "page_down" to KeyEvent.KEYCODE_PAGE_DOWN,
        "insert" to KeyEvent.KEYCODE_INSERT,
        "caps_lock" to KeyEvent.KEYCODE_CAPS_LOCK,
        "media_play_pause" to KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE,
        "media_next" to KeyEvent.KEYCODE_MEDIA_NEXT,
        "media_previous" to KeyEvent.KEYCODE_MEDIA_PREVIOUS,
        "media_volume_up" to KeyEvent.KEYCODE_VOLUME_UP,
        "media_volume_down" to KeyEvent.KEYCODE_VOLUME_DOWN,
        "media_volume_mute" to KeyEvent.KEYCODE_VOLUME_MUTE,
    ) + (1..12).associate { "f$it" to (KeyEvent.KEYCODE_F1 + it - 1) }

    private val punctuation = mapOf(
        ',' to KeyEvent.KEYCODE_COMMA, '.' to KeyEvent.KEYCODE_PERIOD, '/' to KeyEvent.KEYCODE_SLASH,
        ';' to KeyEvent.KEYCODE_SEMICOLON, '\'' to KeyEvent.KEYCODE_APOSTROPHE, '[' to KeyEvent.KEYCODE_LEFT_BRACKET,
        ']' to KeyEvent.KEYCODE_RIGHT_BRACKET, '\\' to KeyEvent.KEYCODE_BACKSLASH, '-' to KeyEvent.KEYCODE_MINUS,
        '=' to KeyEvent.KEYCODE_EQUALS, '`' to KeyEvent.KEYCODE_GRAVE, ' ' to KeyEvent.KEYCODE_SPACE,
    )

    enum class Mod(val meta: Int) {
        SHIFT(KeyEvent.META_SHIFT_ON or KeyEvent.META_SHIFT_LEFT_ON),
        // Android's shortcuts (copy, paste, select all) are on Ctrl, and a
        // Mac's are on Cmd: both drive Ctrl here.
        CTRL(KeyEvent.META_CTRL_ON or KeyEvent.META_CTRL_LEFT_ON),
        ALT(KeyEvent.META_ALT_ON or KeyEvent.META_ALT_LEFT_ON),
    }

    fun modifier(name: String): Mod? = when (name) {
        "shift", "shift_l", "shift_r" -> Mod.SHIFT
        "ctrl", "ctrl_l", "ctrl_r", "cmd", "cmd_l", "cmd_r" -> Mod.CTRL
        "alt", "alt_l", "alt_r", "alt_gr" -> Mod.ALT
        else -> null
    }

    fun isPrintable(name: String) = name.length == 1 || (name.length == 2 && Character.isSurrogatePair(name[0], name[1]))

    /** The key code for a special key, or a character typed with a shortcut. */
    fun code(name: String): Int? {
        special[name]?.let { return it }
        if (name.length != 1) return null
        val c = name[0].lowercaseChar()
        return when (c) {
            in 'a'..'z' -> KeyEvent.KEYCODE_A + (c - 'a')
            in '0'..'9' -> KeyEvent.KEYCODE_0 + (c - '0')
            else -> punctuation[c]
        }
    }
}
