package io.github.orimovo.curslink

import android.content.Context
import android.content.res.Configuration
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PorterDuff
import android.graphics.PorterDuffXfermode
import android.graphics.RectF
import android.graphics.Shader
import android.graphics.Typeface
import android.os.Build
import android.os.SystemClock
import io.github.orimovo.curslink.core.EdgeCommand
import kotlin.math.max
import kotlin.math.min

/**
 * The edge indicator on the phone (see curslink.edgehint on the computer):
 * a soft bar of light along an edge that leads to the computer, brightest by
 * the pointer and fading out at both ends, with a Material 3 label showing
 * the computer's icon and name. The bar alone shows on the edge the pointer
 * is heading for. Drawn by [PointerView], under the pointer.
 */
class EdgeGlow(private val context: Context, private val toPx: (Double, Double) -> Pair<Float, Float>) {
    private val d = context.resources.displayMetrics.density
    private var cmd: EdgeCommand? = null
    private var target = 0f
    private var level = 0f
    private var flashAt = -1L
    private var boost = 0f
    private var last = 0L

    /** Still fading in or out: the view should draw again next frame. */
    val animating get() = level != target || flashAt >= 0

    fun take(c: EdgeCommand) {
        when {
            c.hide -> target = 0f
            c.flash -> {
                if (cmd != null && level > 0.02f) flashAt = SystemClock.uptimeMillis()
                target = 0f
            }
            else -> {
                cmd = c
                flashAt = -1L
                target = c.near.toFloat().coerceIn(0f, 1f)
            }
        }
    }

    private fun step() {
        val now = SystemClock.uptimeMillis()
        val dt = if (last == 0L) 16f else min(64f, (now - last).toFloat())
        last = now
        boost = 0f
        if (flashAt >= 0) {
            val t = (now - flashAt) / FLASH_MS
            if (t >= 1f) { flashAt = -1L; level = 0f } else { boost = 1f - t; level = 1f - t * t }
        } else {
            val k = 1f - Math.pow(1.0 - 0.28, (dt / 16f).toDouble()).toFloat()
            level += (target - level) * k
            if (kotlin.math.abs(target - level) < 0.01f) level = target
        }
    }

    private val dark get() =
        (context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) == Configuration.UI_MODE_NIGHT_YES

    private fun accent(): Int = if (Build.VERSION.SDK_INT >= 31) {
        context.getColor(if (dark) android.R.color.system_accent1_200 else android.R.color.system_accent1_600)
    } else if (dark) Color.rgb(208, 188, 255) else Color.rgb(103, 80, 164)

    private fun withAlpha(c: Int, a: Float) = Color.argb((a.coerceIn(0f, 1f) * 255).toInt(), Color.red(c), Color.green(c), Color.blue(c))

    fun draw(canvas: Canvas, width: Int, height: Int) {
        step()
        val c = cmd ?: return
        if (level <= 0.003f || c.rect.size != 4) return
        // The command's numbers are in the layout we reported (dp); map them to pixels.
        val (x0, y0) = toPx(c.rect[0], c.rect[1])
        val (x1, y1) = toPx(c.rect[0] + c.rect[2], c.rect[1] + c.rect[3])
        val sx = if (c.rect[2] > 0) (x1 - x0) / c.rect[2].toFloat() else 1f
        val sy = if (c.rect[3] > 0) (y1 - y0) / c.rect[3].toFloat() else 1f
        val vertical = c.side == "left" || c.side == "right"
        val s = if (vertical) sy else sx
        val a = min(c.a, c.b).toFloat() * s
        val b = max(c.a, c.b).toFloat() * s
        val at = c.at.toFloat() * s
        drawBar(canvas, c.side, a, b, at, width.toFloat(), height.toFloat())
        if (c.role == "from") drawLabel(canvas, c, at, width.toFloat(), height.toFloat())
    }

    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val dstIn = PorterDuffXfermode(PorterDuff.Mode.DST_IN)

    private fun drawBar(canvas: Canvas, side: String, a: Float, b: Float, at: Float, w: Float, h: Float) {
        val thick = THICK_DP * d
        val r = when (side) {
            "left" -> RectF(0f, a, thick, b)
            "right" -> RectF(w - thick, a, w, b)
            "top" -> RectF(a, 0f, b, thick)
            else -> RectF(a, h - thick, b, h)
        }
        val strength = min(1f, level * (0.9f + 0.5f * boost))
        val col = accent()
        val save = canvas.saveLayer(r, null)
        val hazeA = (if (dark) 0.58f else 0.5f) + 0.3f * boost
        val (gx0, gy0, gx1, gy1) = when (side) {
            "left" -> listOf(r.left, 0f, r.right, 0f)
            "right" -> listOf(r.right, 0f, r.left, 0f)
            "top" -> listOf(0f, r.top, 0f, r.bottom)
            else -> listOf(0f, r.bottom, 0f, r.top)
        }
        paint.shader = LinearGradient(gx0, gy0, gx1, gy1,
            intArrayOf(withAlpha(col, hazeA), withAlpha(col, hazeA * 0.45f), withAlpha(col, 0f)),
            floatArrayOf(0f, 0.25f, 0.85f), Shader.TileMode.CLAMP)
        paint.xfermode = null
        canvas.drawRect(r, paint)
        paint.shader = null
        for ((width, alpha) in listOf(16f to 0.28f, 8f to 0.55f, 3f to 1f)) {
            val wpx = width * d / 2f
            paint.color = withAlpha(col, alpha)
            val core = when (side) {
                "left" -> RectF(0f, a, wpx, b)
                "right" -> RectF(w - wpx, a, w, b)
                "top" -> RectF(a, 0f, b, wpx)
                else -> RectF(a, h - wpx, b, h)
            }
            canvas.drawRoundRect(core, wpx / 2, wpx / 2, paint)
        }
        // Fade along the length: out at both ends, brightest by the pointer.
        val len = b - a
        val stops = lengthStops(len, at - a, 200f * d)
        val vertical = side == "left" || side == "right"
        paint.shader = LinearGradient(
            if (vertical) 0f else a, if (vertical) a else 0f, if (vertical) 0f else b, if (vertical) b else 0f,
            stops.map { Color.argb((it.second * strength * 255).toInt(), 0, 0, 0) }.toIntArray(),
            stops.map { it.first }.toFloatArray(), Shader.TileMode.CLAMP)
        paint.xfermode = dstIn
        canvas.drawRect(r, paint)
        paint.xfermode = null
        paint.shader = null
        canvas.restoreToCount(save)
    }

    private val namePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 14f * d * context.resources.configuration.fontScale.coerceAtMost(1.3f)
        typeface = Typeface.create(Typeface.DEFAULT, Typeface.BOLD)
    }
    private val subPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = 11.5f * d * context.resources.configuration.fontScale.coerceAtMost(1.3f)
    }
    private val stroke = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeCap = Paint.Cap.ROUND; strokeJoin = Paint.Join.ROUND
    }

    private fun drawLabel(canvas: Canvas, c: EdgeCommand, at: Float, w: Float, h: Float) {
        val shown = if (level > 0.05f) min(1f, level * 1.15f) else return
        val sub = KIND_TEXT[c.kind] ?: ""
        val chip = 34f * d
        val pad = 6f * d
        val textW = max(namePaint.measureText(c.name), subPaint.measureText(sub))
        val lw = pad + chip + 10f * d + textW + 10f * d + 8f * d + 12f * d
        val lh = chip + 2 * pad
        val gap = 10f * d
        val slide = (1f - min(1f, level)) * 14f * d
        val left: Float
        val top: Float
        when (c.side) {
            "left", "right" -> {
                top = (at - 22f * d - lh).coerceIn(8f * d, h - lh - 8f * d)
                left = if (c.side == "left") gap - slide else w - gap - lw + slide
            }
            else -> {
                var l = at + 26f * d
                if (l + lw > w - 8f * d) l = at - 26f * d - lw
                left = l.coerceIn(8f * d, max(8f * d, w - lw - 8f * d))
                top = if (c.side == "top") gap - slide else h - gap - lh + slide
            }
        }
        val body = RectF(left, top, left + lw, top + lh)
        val alpha = (shown * 255).toInt()
        // Shadow, then the tonal pill (a translucent surface: overlays can't blur what's behind them).
        paint.color = Color.argb((alpha * if (dark) 0.35f else 0.14f).toInt(), 0, 0, 0)
        canvas.drawRoundRect(RectF(body.left, body.top + 3f * d, body.right, body.bottom + 4f * d), lh / 2, lh / 2, paint)
        paint.color = if (dark) Color.argb((alpha * 0.92f).toInt(), 43, 41, 48) else Color.argb((alpha * 0.92f).toInt(), 243, 237, 247)
        canvas.drawRoundRect(body, lh / 2, lh / 2, paint)
        stroke.strokeWidth = 1f * d
        stroke.color = if (dark) Color.argb(alpha / 10, 255, 255, 255) else Color.argb(alpha / 2, 255, 255, 255)
        canvas.drawRoundRect(body, lh / 2, lh / 2, stroke)
        val mirrored = c.side == "left"
        val cy = body.centerY()
        val chipX = if (mirrored) body.right - pad - chip else body.left + pad
        val acc = accent()
        paint.color = if (dark) Color.argb(alpha, 74, 68, 88) else Color.argb(alpha, 232, 222, 248)
        canvas.drawCircle(chipX + chip / 2, cy, chip / 2, paint)
        stroke.color = withAlpha(acc, shown)
        stroke.strokeWidth = 1.8f * d
        canvas.save()
        canvas.translate(chipX + chip / 2 - 9.5f * d, cy - 9.5f * d)
        canvas.drawPath(icon(c.kind, 19f * d), stroke)
        canvas.restore()
        val ink = if (dark) Color.rgb(230, 224, 233) else Color.rgb(29, 27, 32)
        val textX = if (mirrored) body.left + pad + 20f * d else chipX + chip + 10f * d
        namePaint.color = withAlpha(ink, shown)
        subPaint.color = withAlpha(ink, shown * 0.7f)
        canvas.drawText(c.name, textX, cy - 2f * d, namePaint)
        canvas.drawText(sub, textX, cy + 13f * d, subPaint)
        stroke.color = withAlpha(ink, shown * 0.6f)
        val chev = Path()
        val cs = 4.5f * d
        var x = if (mirrored) body.left + pad + 6f * d else body.right - pad - 6f * d
        when (c.side) {
            "left" -> { chev.moveTo(x + cs, cy - cs); chev.lineTo(x, cy); chev.lineTo(x + cs, cy + cs) }
            "top" -> { x -= cs / 2; chev.moveTo(x - cs, cy + cs / 2); chev.lineTo(x, cy - cs / 2); chev.lineTo(x + cs, cy + cs / 2) }
            "bottom" -> { x -= cs / 2; chev.moveTo(x - cs, cy - cs / 2); chev.lineTo(x, cy + cs / 2); chev.lineTo(x + cs, cy - cs / 2) }
            else -> { chev.moveTo(x - cs, cy - cs); chev.lineTo(x, cy); chev.lineTo(x - cs, cy + cs) }
        }
        canvas.drawPath(chev, stroke)
    }

    private fun icon(kind: String, size: Float): Path {
        val s = size / 24f
        val p = Path()
        when (kind) {
            "phone" -> {
                p.addRoundRect(RectF(6.5f * s, 2.5f * s, 17.5f * s, 21.5f * s), 2.4f * s, 2.4f * s, Path.Direction.CW)
                p.moveTo(10.5f * s, 18.5f * s); p.lineTo(13.5f * s, 18.5f * s)
            }
            "mac" -> {
                p.addRoundRect(RectF(4f * s, 4.5f * s, 20f * s, 15.5f * s), 1.6f * s, 1.6f * s, Path.Direction.CW)
                p.moveTo(3.5f * s, 15.5f * s); p.lineTo(2f * s, 19.5f * s); p.lineTo(22f * s, 19.5f * s); p.lineTo(20.5f * s, 15.5f * s)
            }
            else -> {
                p.addRoundRect(RectF(2.5f * s, 3.5f * s, 21.5f * s, 16.5f * s), 2f * s, 2f * s, Path.Direction.CW)
                p.moveTo(8f * s, 20.5f * s); p.lineTo(16f * s, 20.5f * s)
                p.moveTo(12f * s, 16.5f * s); p.lineTo(12f * s, 20.5f * s)
            }
        }
        return p
    }

    companion object {
        private const val THICK_DP = 48f
        private const val FLASH_MS = 450f
        private val KIND_TEXT = mapOf("mac" to "Mac", "windows" to "Windows PC", "linux" to "Linux PC", "phone" to "Android phone")

        /** Alpha along the bar: 0 at both ends, 1 by the pointer (same as curslink.edgeoverlay.length_stops). */
        fun lengthStops(length: Float, at: Float, focus: Float, base: Float = 0.25f): List<Pair<Float, Float>> {
            if (length <= 0f) return listOf(0f to 0f, 1f to 0f)
            val p = (at / length).coerceIn(0.04f, 0.96f)
            val f = focus / length
            val points = sortedMapOf(0f to 0f, 1f to 0f, p to 1f)
            for ((pos, alpha) in listOf(0.14f to base, 0.86f to base, (p - f) to 0.45f, (p + f) to 0.45f)) {
                if (pos > 0.02f && pos < 0.98f && kotlin.math.abs(pos - p) > 0.01f) points.putIfAbsent(pos, alpha)
            }
            return points.map { (pos, alpha) ->
                val cap = min(pos, 1f - pos) / 0.14f
                pos to min(alpha, max(cap, if (pos == p) 0.35f else 0f))
            }
        }
    }
}
