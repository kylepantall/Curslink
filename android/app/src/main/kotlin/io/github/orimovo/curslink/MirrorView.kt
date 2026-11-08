package io.github.orimovo.curslink

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.graphics.Shader
import android.os.SystemClock
import android.view.View

/**
 * Mirror mode's mirrors on the phone: where the phone stands in front of
 * another screen on the layout, it shows the part of that screen it covers
 * while that screen is in use (see core/Mirror.kt). Each one fades in, flashes
 * its inner edges, shows a small glass tag with the other device's name for a
 * few seconds, and draws that screen's pointer. Fills the screen, draws only
 * the mirrors, and lets every touch through. Main thread only.
 */
class MirrorView(context: Context) : View(context) {
    private class Mirror(var rect: RectF, var label: String, var edges: Set<String>) {
        var bitmap: Bitmap? = null
        var shownAt = 0L
        var closingAt = 0L
        var pointer: Pair<Float, Float>? = null
    }

    private val d = resources.displayMetrics.density
    private val mirrors = LinkedHashMap<String, Mirror>()
    private val pix = Paint(Paint.FILTER_BITMAP_FLAG or Paint.ANTI_ALIAS_FLAG)
    private val line = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }
    private val tagFill = Paint(Paint.ANTI_ALIAS_FLAG)
    private val tagEdge = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE; strokeWidth = 1f * d; color = Color.argb(110, 255, 255, 255) }
    private val text = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; textSize = 12f * d; isFakeBoldText = true
    }
    private val arrowFill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; style = Paint.Style.FILL }
    private val arrowEdge = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.rgb(17, 17, 17); style = Paint.Style.STROKE; strokeWidth = 1.3f * d; strokeJoin = Paint.Join.ROUND
    }
    private val accent = Color.rgb(63, 169, 255)
    private val arrow = Path().apply {
        moveTo(0f, 0f); lineTo(0f, 17.5f * d); lineTo(4.4f * d, 13.4f * d); lineTo(7.4f * d, 20.6f * d)
        lineTo(10.4f * d, 19.4f * d); lineTo(7.5f * d, 12.4f * d); lineTo(13.2f * d, 12.4f * d); close()
    }

    val showing: Boolean get() = mirrors.values.any { it.bitmap != null && it.closingAt == 0L }

    /** The mirrors wanted now, with their rectangles in pixels on the screen. */
    fun set(wanted: Map<String, Triple<RectF, String, Set<String>>>) {
        val now = SystemClock.uptimeMillis()
        for ((id, m) in mirrors) if (id !in wanted && m.closingAt == 0L) m.closingAt = now
        for ((id, w) in wanted) {
            val m = mirrors[id]
            if (m == null || m.closingAt != 0L) mirrors[id] = Mirror(w.first, w.second, w.third)
            else { m.rect = w.first; m.label = w.second; m.edges = w.third }
        }
        invalidate()
    }

    fun image(id: String, bitmap: Bitmap) {
        val m = mirrors[id] ?: return
        if (m.closingAt != 0L) return
        if (m.bitmap == null) m.shownAt = SystemClock.uptimeMillis()
        m.bitmap?.recycle()
        m.bitmap = bitmap
        invalidate()
    }

    fun pointer(id: String, x: Float?, y: Float?) {
        val m = mirrors[id] ?: return
        m.pointer = if (x == null || y == null) null else x to y
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        val now = SystemClock.uptimeMillis()
        var animating = false
        val gone = mutableListOf<String>()
        for ((id, m) in mirrors) {
            if (m.closingAt != 0L && m.bitmap == null) { gone += id; continue }
            val bmp = m.bitmap ?: continue
            val r = m.rect
            val alpha = if (m.closingAt != 0L) {
                val k = (now - m.closingAt) / FADE_MS
                if (k >= 1f) { gone += id; continue }
                1f - k
            } else minOf(1f, (now - m.shownAt) / FADE_MS)
            val age = now - m.shownAt
            if (alpha < 1f || age < TAG_MS + 600) animating = true
            pix.alpha = (alpha * 255).toInt()
            canvas.drawBitmap(bmp, null, r, pix)
            drawEdges(canvas, m, alpha, age)
            m.pointer?.let { (px, py) -> drawPointer(canvas, r.left + px * r.width(), r.top + py * r.height(), alpha) }
            if (age < TAG_MS + 500) drawTag(canvas, m, alpha * (if (age < TAG_MS) 1f else maxOf(0f, 1f - (age - TAG_MS) / 500f)))
        }
        gone.forEach { mirrors.remove(it)?.bitmap?.recycle() }
        if (animating) postInvalidateOnAnimation()
    }

    private fun drawEdges(c: Canvas, m: Mirror, alpha: Float, age: Long) {
        val r = m.rect
        val lines = mapOf(
            "left" to floatArrayOf(r.left, r.top, r.left, r.bottom),
            "right" to floatArrayOf(r.right, r.top, r.right, r.bottom),
            "top" to floatArrayOf(r.left, r.top, r.right, r.top),
            "bottom" to floatArrayOf(r.left, r.bottom, r.right, r.bottom),
        )
        for (side in m.edges) {
            val p = lines[side] ?: continue
            line.color = Color.BLACK; line.alpha = (60 * alpha).toInt(); line.strokeWidth = 3f * d
            c.drawLine(p[0], p[1], p[2], p[3], line)
            line.color = Color.WHITE; line.alpha = (200 * alpha).toInt(); line.strokeWidth = 1.5f * d
            c.drawLine(p[0], p[1], p[2], p[3], line)
            if (age < FLASH_MS) {
                line.color = accent; line.alpha = (255 * (1f - age / FLASH_MS.toFloat()) * alpha).toInt(); line.strokeWidth = 4f * d
                c.drawLine(p[0], p[1], p[2], p[3], line)
            }
        }
    }

    private fun drawPointer(c: Canvas, x: Float, y: Float, alpha: Float) {
        arrowFill.alpha = (alpha * 255).toInt()
        arrowEdge.alpha = arrowFill.alpha
        c.save()
        c.translate(x, y)
        c.drawPath(arrow, arrowFill)
        c.drawPath(arrow, arrowEdge)
        c.restore()
    }

    private fun drawTag(c: Canvas, m: Mirror, alpha: Float) {
        if (alpha <= 0f) return
        val label = "${m.label.ifEmpty { "Behind" }} · live"
        val w = text.measureText(label) + 30f * d
        val h = 24f * d
        val r = m.rect
        if (w + 16f * d > r.width() || h + 16f * d > r.height()) return
        val tag = RectF(r.right - w - 10f * d, r.top + 10f * d, r.right - 10f * d, r.top + 10f * d + h)
        tagFill.shader = LinearGradient(0f, tag.top, 0f, tag.bottom, Color.argb(175, 60, 60, 70), Color.argb(175, 25, 25, 30), Shader.TileMode.CLAMP)
        val a = (alpha * 255).toInt()
        tagFill.alpha = a; tagEdge.alpha = (110 * alpha).toInt(); text.alpha = a
        c.drawRoundRect(tag, h / 2, h / 2, tagFill)
        c.drawRoundRect(tag, h / 2, h / 2, tagEdge)
        line.style = Paint.Style.FILL; line.color = accent; line.alpha = a
        c.drawCircle(tag.left + 12f * d, tag.centerY(), 3f * d, line)
        line.style = Paint.Style.STROKE
        c.drawText(label, tag.left + 20f * d, tag.centerY() - (text.ascent() + text.descent()) / 2, text)
    }

    companion object {
        private const val FADE_MS = 220f
        private const val FLASH_MS = 600L
        private const val TAG_MS = 3500L
    }
}
