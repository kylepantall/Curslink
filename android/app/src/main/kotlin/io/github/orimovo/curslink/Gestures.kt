package io.github.orimovo.curslink

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.PointF
import android.os.Handler
import android.os.SystemClock

/**
 * One finger, driven by the mouse: pressing the left button puts it down
 * where the pointer is, moving drags it, letting go lifts it. So a click is
 * a tap, holding still is a long press, and press-and-move drags.
 *
 * Scrolling uses the same finger: it goes down at the pointer, slides by
 * the wheel's (or trackpad's) movement, and lifts after a short pause,
 * holding still first so the page doesn't fling on.
 *
 * Built on continued strokes (Android 8+): each piece of the finger's path
 * is dispatched when the one before it has played.
 */
class Gestures(private val service: AccessibilityService, private val main: Handler) {
    private var stroke: GestureDescription.StrokeDescription? = null
    private var last = PointF()
    private var target: PointF? = null
    private var lifting = false
    private var inFlight = false
    private var lastSent = 0L
    var scrolling = false
        private set
    val down: Boolean get() = stroke != null

    /** Finger down at [p] (pixels). */
    fun press(p: PointF, forScroll: Boolean = false) {
        if (stroke != null) cancel()
        scrolling = forScroll
        lifting = false
        target = null
        val path = Path().apply { moveTo(p.x, p.y) }
        val s = GestureDescription.StrokeDescription(path, 0, 1, true)
        stroke = s
        last = PointF(p.x, p.y)
        dispatch(s)
    }

    /** A long press at [p]: what a right-click means on a touch screen (a context menu, selecting text). */
    fun longPress(p: PointF, holdMs: Long) {
        if (stroke != null) return
        val path = Path().apply { moveTo(p.x, p.y) }
        val s = GestureDescription.StrokeDescription(path, 0, holdMs, false)
        service.dispatchGesture(GestureDescription.Builder().addStroke(s).build(), null, main)
    }

    fun moveTo(p: PointF) {
        if (stroke == null) return
        target = PointF(p.x, p.y)
        pump()
    }

    /** Lift the finger at [p], after holding still [holdMs] (no fling). */
    fun release(p: PointF? = null, holdMs: Long = 0) {
        if (stroke == null) return
        if (p != null) target = PointF(p.x, p.y)
        lifting = true
        hold = holdMs
        pump()
    }

    private var hold = 0L

    fun cancel() {
        stroke = null
        inFlight = false
        target = null
        lifting = false
        scrolling = false
    }

    private fun pump() {
        val s = stroke ?: return
        if (inFlight) return
        val to = target
        if (to == null && !lifting) return
        target = null
        val now = SystemClock.uptimeMillis()
        val path = Path().apply {
            moveTo(last.x, last.y)
            if (to != null && (to.x != last.x || to.y != last.y)) lineTo(to.x, to.y)
        }
        val moving = to != null && (to.x != last.x || to.y != last.y)
        val duration = if (moving) (now - lastSent).coerceIn(8, 40) else maxOf(hold, 1)
        val end = lifting && !(moving && hold > 0)
        val next = s.continueStroke(path, 0, duration, !end)
        if (to != null) last = to
        if (end) {
            stroke = null
            scrolling = false
            lifting = false
            hold = 0
        } else {
            stroke = next
        }
        dispatch(next)
    }

    private fun dispatch(s: GestureDescription.StrokeDescription) {
        inFlight = true
        lastSent = SystemClock.uptimeMillis()
        val ok = service.dispatchGesture(GestureDescription.Builder().addStroke(s).build(), object : AccessibilityService.GestureResultCallback() {
            override fun onCompleted(g: GestureDescription) {
                inFlight = false
                pump()
            }

            override fun onCancelled(g: GestureDescription) {
                // Something else touched the screen, or the system took over.
                cancel()
            }
        }, main)
        if (!ok) cancel()
    }
}
