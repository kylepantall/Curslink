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
 * the mirrors, and lets every touch through. While one is being lined up it
 * is outlined and draws the alignment guides (the same lines the screen
 * behind shows). Touching the phone puts the mirrors away until it has been
 * left alone for a few seconds. Main thread only.
 */
class MirrorView(context: Context) : View(context) {
    /** A mirror wanted now: where (pixels on the screen), and while it's lined up, the guides. */
    data class Want(
        val rect: RectF, val label: String, val edges: Set<String>,
        val tune: Boolean = false, val guides: List<Double>? = null, val src: List<Double>? = null,
    )

    private class Mirror(var rect: RectF, var label: String, var edges: Set<String>) {
        var tune = false
        var guides: List<Double>? = null
        var src: List<Double>? = null
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

    private val dash = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 3f * d; color = accent
        pathEffect = android.graphics.DashPathEffect(floatArrayOf(9f * d, 6f * d), 0f)
    }
    private val guide = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.STROKE }

    // Touched by hand: the mirrors stay away until this long after the last touch.
    private var awayUntil = 0L
    private var awayFrom = 0L

    val showing: Boolean get() = mirrors.values.any { it.bitmap != null && it.closingAt == 0L }

    /** Is any mirror there to be put away by a touch? */
    val anyMirror: Boolean get() = mirrors.values.any { it.closingAt == 0L }

    /** The mirrors wanted now. */
    fun set(wanted: Map<String, Want>) {
        val now = SystemClock.uptimeMillis()
        for ((id, m) in mirrors) if (id !in wanted && m.closingAt == 0L) m.closingAt = now
        for ((id, w) in wanted) {
            var m = mirrors[id]
            if (m == null || m.closingAt != 0L) { m = Mirror(w.rect, w.label, w.edges); mirrors[id] = m }
            else { m.rect = w.rect; m.label = w.label; m.edges = w.edges }
            m.tune = w.tune; m.guides = w.guides; m.src = w.src
        }
        // Lining one up: it shows even if the phone was just touched.
        if (wanted.values.any { it.tune }) awayUntil = 0L
        invalidate()
    }

    /** The phone was touched by hand: put the mirrors away while it's in use. */
    fun touchedByHand() {
        val now = SystemClock.uptimeMillis()
        if (mirrors.values.any { it.tune }) return
        if (now >= awayUntil) awayFrom = now
        awayUntil = now + AWAY_MS
        invalidate()
        postInvalidateDelayed(AWAY_MS + 20)
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
            val base = if (m.closingAt != 0L) {
                val k = (now - m.closingAt) / FADE_MS
                if (k >= 1f) { gone += id; continue }
                1f - k
            } else minOf(1f, (now - m.shownAt) / FADE_MS)
            val alpha = base * away(now).also { if (it in 0.001f..0.999f) animating = true }
            if (alpha <= 0f) continue
            val age = now - m.shownAt
            if (alpha < 1f || age < TAG_MS + 600) animating = true
            pix.alpha = (alpha * 255).toInt()
            canvas.drawBitmap(bmp, null, r, pix)
            drawEdges(canvas, m, alpha, age)
            m.pointer?.let { (px, py) -> drawPointer(canvas, r.left + px * r.width(), r.top + py * r.height(), alpha) }
            if (m.tune) {
                drawGuides(canvas, guideRect(m))
                canvas.drawRect(RectF(r.left + 1.5f * d, r.top + 1.5f * d, r.right - 1.5f * d, r.bottom - 1.5f * d), dash)
                drawTag(canvas, m, 1f, "Line up the coloured lines · drag to move · scroll to zoom · drag a side to stretch")
            } else if (age < TAG_MS + 500) drawTag(canvas, m, alpha * (if (age < TAG_MS) 1f else maxOf(0f, 1f - (age - TAG_MS) / 500f)))
        }
        gone.forEach { mirrors.remove(it)?.bitmap?.recycle() }
        if (animating) postInvalidateOnAnimation()
    }

    /** 1 normally; fading to 0 when touched by hand, and back once left alone. */
    private fun away(now: Long): Float {
        if (awayUntil == 0L) return 1f
        if (now < awayUntil) return maxOf(0f, 1f - (now - awayFrom) / FADE_MS)
        val k = (now - awayUntil) / FADE_MS
        if (k >= 1f) { awayUntil = 0L; return 1f }
        return k
    }

    /** Where the guides' area (on the screen behind) falls on this mirror, which shows [Mirror.src] of there. */
    private fun guideRect(m: Mirror): RectF {
        val r = m.rect
        val g = m.guides ?: return r
        val s = m.src ?: return r
        if (s[2] <= 0 || s[3] <= 0) return r
        val kx = r.width() / s[2].toFloat()
        val ky = r.height() / s[3].toFloat()
        return RectF(r.left + (g[0] - s[0]).toFloat() * kx, r.top + (g[1] - s[1]).toFloat() * ky,
            r.left + (g[0] - s[0] + g[2]).toFloat() * kx, r.top + (g[1] - s[1] + g[3]).toFloat() * ky)
    }

    /** The guides (curslink/mirror/helper.py guide_shapes): three lines across and down, both diagonals, a ring. */
    private fun drawGuides(c: Canvas, g: RectF) {
        val w = maxOf(1f, g.width())
        val h = maxOf(1f, g.height())
        val far = 20 * maxOf(w, h) + 10_000f
        val shapes = mutableListOf<Pair<Int, FloatArray>>()
        for (f in floatArrayOf(0.25f, 0.5f, 0.75f)) {
            val y = g.top + f * h
            shapes += GUIDE_H to floatArrayOf(g.left - far, y, g.right + far, y)
            val x = g.left + f * w
            shapes += GUIDE_V to floatArrayOf(x, g.top - far, x, g.bottom + far)
        }
        val n = far / w
        shapes += GUIDE_D to floatArrayOf(g.left - w * n, g.top - h * n, g.right + w * n, g.bottom + h * n)
        shapes += GUIDE_D to floatArrayOf(g.right + w * n, g.top - h * n, g.left - w * n, g.bottom + h * n)
        val ring = (minOf(w, h) / 2 + kotlin.math.hypot(w, h) / 2) / 2
        for (halo in listOf(true, false)) {
            guide.strokeWidth = if (halo) 4.5f * d else 2f * d
            for ((color, p) in shapes) {
                guide.color = if (halo) Color.argb(150, 0, 0, 0) else color
                c.drawLine(p[0], p[1], p[2], p[3], guide)
            }
            guide.color = if (halo) Color.argb(150, 0, 0, 0) else GUIDE_O
            c.drawCircle(g.centerX(), g.centerY(), ring, guide)
        }
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

    private fun drawTag(c: Canvas, m: Mirror, alpha: Float, say: String? = null) {
        if (alpha <= 0f) return
        var label = say ?: "${m.label.ifEmpty { "Behind" }} · live"
        // A small mirror: say as much as fits.
        while (say != null && label.contains(" · ") && text.measureText(label) + 46f * d > m.rect.width()) {
            label = label.substringBeforeLast(" · ")
        }
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
        private const val AWAY_MS = 6000L
        private val GUIDE_H = Color.rgb(56, 225, 255)
        private val GUIDE_V = Color.rgb(255, 79, 216)
        private val GUIDE_D = Color.rgb(255, 214, 10)
        private val GUIDE_O = Color.rgb(93, 255, 122)
    }
}
