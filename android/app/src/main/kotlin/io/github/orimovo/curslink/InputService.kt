package io.github.orimovo.curslink

import android.accessibilityservice.AccessibilityService
import android.graphics.PixelFormat
import android.graphics.PointF
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.view.KeyEvent
import android.view.ViewConfiguration
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import kotlin.math.abs

/**
 * Plays the computer's mouse and keyboard back on the phone. Turned on once
 * in Settings › Accessibility. Calls arrive on the link's threads and are
 * handled on the main thread.
 */
class InputService : AccessibilityService() {
    companion object {
        @Volatile var instance: InputService? = null
            private set
        private const val KEEP_ON_MS = 120_000L
        private const val SHAPE_EVERY_MS = 120L
    }

    private val main = Handler(Looper.getMainLooper())
    private lateinit var gestures: Gestures
    private lateinit var wm: WindowManager
    private var view: PointerView? = null
    private var params: WindowManager.LayoutParams? = null
    private val shapeThread = HandlerThread("curslink-shape").apply { start() }
    private val shapeHandler = Handler(shapeThread.looper)
    private var shapeQueued = false
    private var active = false
    private var lastInput = 0L
    private val mods = mutableSetOf<Keys.Mod>()
    private var pointer = PointF()
    private var finger: PointF? = null
    private var pendingScroll = PointF()
    private val endScroll = Runnable { gestures.release(holdMs = 60); finger = null }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        gestures = Gestures(this, main)
        wm = getSystemService(WINDOW_SERVICE) as WindowManager
        addOverlay()
        // The link may already be up (the service runs on its own).
        if (Prefs(this).pairing != null) runCatching { LinkService.start(this) }
    }

    override fun onDestroy() {
        instance = null
        view?.let { runCatching { wm.removeView(it) } }
        shapeThread.quitSafely()
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    private fun addOverlay() {
        val v = PointerView(this)
        val lp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.MATCH_PARENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE or WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or
                WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN or WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS,
            PixelFormat.TRANSLUCENT,
        )
        if (Build.VERSION.SDK_INT >= 28) {
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
        }
        runCatching { wm.addView(v, lp) }.onSuccess {
            view = v
            params = lp
        }
    }

    // ----------------------------------------------------------- mapping
    /** Host coordinates (the dp layout we reported) to pixels on the screen as it's turned now. */
    private fun toPx(x: Double, y: Double): PointF {
        val size = Screen.sizePx(this)
        val (w, h) = Prefs(this).layoutSize ?: Screen.naturalSizeDp(this)
        val px = (x / w * size.x).toFloat().coerceIn(0f, size.x - 1f)
        val py = (y / h * size.y).toFloat().coerceIn(0f, size.y - 1f)
        return PointF(px, py)
    }

    private fun touched() {
        lastInput = SystemClock.uptimeMillis()
        keepScreenOn(true)
    }

    /** Keep the screen on while the mouse is here and in use; let it sleep when idle. */
    private fun keepScreenOn(on: Boolean) {
        val lp = params ?: return
        val v = view ?: return
        val flag = WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
        val want = if (on) lp.flags or flag else lp.flags and flag.inv()
        if (want != lp.flags) {
            lp.flags = want
            runCatching { wm.updateViewLayout(v, lp) }
        }
        main.removeCallbacks(idle)
        if (on) main.postDelayed(idle, KEEP_ON_MS)
    }

    private val idle = Runnable { keepScreenOn(false) }

    private fun wakeIfDark() {
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        if (!pm.isInteractive) Waker.wake(this)
    }

    // -------------------------------------------------------- from the link
    fun enter(x: Double, y: Double, effect: String) = main.post {
        active = true
        pointer = toPx(x, y)
        view?.apply {
            moveTo(pointer.x, pointer.y)
            shown = true
            when (effect) {
                "pop" -> pop()
                "blink" -> blink()
            }
        }
        touched()
        queueShape()
    }

    fun leave(effect: String) = main.post {
        active = false
        if (gestures.down) gestures.release(pointer)
        finger = null
        mods.clear()
        view?.apply {
            if (effect == "pop") pop()
            shown = false
        }
        keepScreenOn(false)
    }

    fun move(x: Double, y: Double) = main.post {
        if (!active) return@post
        pointer = toPx(x, y)
        view?.moveTo(pointer.x, pointer.y)
        if (gestures.down && !gestures.scrolling) gestures.moveTo(pointer)
        touched()
        queueShape()
    }

    fun button(name: String, pressed: Boolean) = main.post {
        if (!active) return@post
        touched()
        if (pressed) wakeIfDark()
        when (name) {
            "left" -> {
                if (pressed) {
                    main.removeCallbacks(endScroll)
                    gestures.press(pointer)
                } else {
                    gestures.release(pointer)
                }
            }
            // Android has no right-click: it's Back, as with a mouse plugged in.
            "right", "x1" -> if (pressed) performGlobalAction(GLOBAL_ACTION_BACK)
            "middle" -> if (pressed) performGlobalAction(GLOBAL_ACTION_HOME)
            "x2" -> if (pressed) performGlobalAction(GLOBAL_ACTION_RECENTS)
        }
    }

    fun scroll(dx: Double, dy: Double) = main.post {
        if (!active) return@post
        touched()
        if (gestures.down && !gestures.scrolling) return@post  // dragging: the wheel does nothing
        val step = ViewConfiguration.get(this).scaledVerticalScrollFactor
        // Wheel up shows what's above: the finger slides down the page.
        val ddx = (-dx * step).toFloat()
        val ddy = (dy * step).toFloat()
        val size = Screen.sizePx(this)
        var f = finger
        if (f == null || !gestures.down) {
            pendingScroll.offset(ddx, ddy)
            val slop = ViewConfiguration.get(this).scaledTouchSlop
            if (abs(pendingScroll.x) < slop && abs(pendingScroll.y) < slop) return@post
            gestures.press(pointer, forScroll = true)
            f = PointF(pointer.x, pointer.y)
            f.offset(pendingScroll.x, pendingScroll.y)
            pendingScroll = PointF()
        } else {
            f = PointF(f.x + ddx, f.y + ddy)
        }
        if (f.x < 0 || f.y < 0 || f.x >= size.x || f.y >= size.y) {
            // The finger ran off the screen: lift it; the next scroll starts again at the pointer.
            gestures.release(PointF(f.x.coerceIn(0f, size.x - 1f), f.y.coerceIn(0f, size.y - 1f)), holdMs = 60)
            finger = null
            return@post
        }
        finger = f
        gestures.moveTo(f)
        main.removeCallbacks(endScroll)
        main.postDelayed(endScroll, 150)
    }

    fun key(name: String, pressed: Boolean) = main.post {
        if (!active) return@post
        touched()
        Keys.modifier(name)?.let { mod ->
            if (pressed) mods.add(mod) else mods.remove(mod)
            return@post
        }
        if (!pressed) return@post  // a held key repeats as presses
        wakeIfDark()
        if (name == "esc") {
            performGlobalAction(GLOBAL_ACTION_BACK)
            return@post
        }
        val shortcut = Keys.Mod.CTRL in mods || Keys.Mod.ALT in mods
        if (Keys.isPrintable(name) && !shortcut) {
            type(name)
            return@post
        }
        val code = Keys.code(name) ?: return@post
        sendKey(code)
    }

    // ------------------------------------------------------------ typing
    private fun type(text: String) {
        if (Build.VERSION.SDK_INT >= 33 && Ime.commit(this, text)) return
        editFocused { current, start, end -> Triple(current.substring(0, start) + text + current.substring(end), start + text.length, true) }
    }

    private fun sendKey(code: Int) {
        val meta = mods.fold(0) { m, mod -> m or mod.meta }
        if (Build.VERSION.SDK_INT >= 33 && Ime.key(this, code, meta)) return
        // Before Android 13: just the editing keys, on the focused text field.
        when (code) {
            KeyEvent.KEYCODE_DEL -> editFocused { t, s, e ->
                if (s != e) Triple(t.substring(0, s) + t.substring(e), s, true)
                else if (s > 0) Triple(t.substring(0, s - 1) + t.substring(e), s - 1, true) else null
            }
            KeyEvent.KEYCODE_FORWARD_DEL -> editFocused { t, s, e ->
                if (s != e) Triple(t.substring(0, s) + t.substring(e), s, true)
                else if (e < t.length) Triple(t.substring(0, s) + t.substring(e + 1), s, true) else null
            }
            KeyEvent.KEYCODE_ENTER -> if (Build.VERSION.SDK_INT >= 30) {
                focusedEditable()?.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id)
            }
            KeyEvent.KEYCODE_SPACE -> type(" ")
        }
    }

    private fun focusedEditable(): AccessibilityNodeInfo? =
        rootInActiveWindow?.findFocus(AccessibilityNodeInfo.FOCUS_INPUT)?.takeIf { it.isEditable }

    /** Set the focused field's text: [edit] (text, selection start, end) -> (new text, cursor, changed). */
    private fun editFocused(edit: (String, Int, Int) -> Triple<String, Int, Boolean>?) {
        val node = focusedEditable() ?: return
        val current = if (node.isShowingHintText) "" else node.text?.toString() ?: ""
        var s = node.textSelectionStart.takeIf { it >= 0 } ?: current.length
        var e = node.textSelectionEnd.takeIf { it >= 0 } ?: current.length
        if (s > e) s = e.also { e = s }
        s = s.coerceIn(0, current.length)
        e = e.coerceIn(0, current.length)
        val (text, cursor, _) = edit(current, s, e) ?: return
        node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        })
        node.performAction(AccessibilityNodeInfo.ACTION_SET_SELECTION, Bundle().apply {
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_START_INT, cursor)
            putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_SELECTION_END_INT, cursor)
        })
    }

    // ------------------------------------------------------ pointer shape
    /** What's under the pointer decides its shape: I-beam on text, hand on
     *  something to tap. Looked up off the main thread, a few times a second. */
    private fun queueShape() {
        if (shapeQueued) return
        shapeQueued = true
        shapeHandler.postDelayed({
            shapeQueued = false
            val p = PointF(pointer.x, pointer.y)
            val shape = runCatching { shapeAt(p.x.toInt(), p.y.toInt()) }.getOrDefault(PointerView.Shape.ARROW)
            main.post { view?.shape = shape }
        }, SHAPE_EVERY_MS)
    }

    private fun shapeAt(x: Int, y: Int): PointerView.Shape {
        val r = Rect()
        val ws = windows
        for (w in ws) {
            if (w.type == AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY) continue
            w.getBoundsInScreen(r)
            if (!r.contains(x, y)) continue
            val root = w.root ?: continue
            val hit = deepest(root, x, y, 0) ?: return PointerView.Shape.ARROW
            return when {
                hit.first -> PointerView.Shape.TEXT
                hit.second -> PointerView.Shape.HAND
                else -> PointerView.Shape.ARROW
            }
        }
        return PointerView.Shape.ARROW
    }

    /** (editable, tappable) for the innermost node at x, y. */
    private fun deepest(node: AccessibilityNodeInfo, x: Int, y: Int, depth: Int): Pair<Boolean, Boolean>? {
        val r = Rect()
        node.getBoundsInScreen(r)
        if (!r.contains(x, y) || !node.isVisibleToUser) return null
        if (depth < 40) {
            for (i in node.childCount - 1 downTo 0) {
                val child = node.getChild(i) ?: continue
                deepest(child, x, y, depth + 1)?.let { inner ->
                    // Inside something tappable (a list row) counts as tappable.
                    return inner.first to (inner.second || node.isClickable)
                }
            }
        }
        return node.isEditable to (node.isClickable || node.isLongClickable || node.isCheckable)
    }
}

/** Typing as an input method beside the real keyboard (Android 13+): the
 *  service is attached to whichever text field has focus. */
@androidx.annotation.RequiresApi(33)
private object Ime {
    fun commit(service: AccessibilityService, text: String): Boolean {
        val ic = service.inputMethod?.currentInputConnection ?: return false
        ic.commitText(text, 1, null)
        return true
    }

    fun key(service: AccessibilityService, code: Int, meta: Int): Boolean {
        val ic = service.inputMethod?.currentInputConnection ?: return false
        val now = SystemClock.uptimeMillis()
        ic.sendKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_DOWN, code, 0, meta))
        ic.sendKeyEvent(KeyEvent(now, now, KeyEvent.ACTION_UP, code, 0, meta))
        return true
    }
}
