package io.github.orimovo.curslink

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.graphics.RectF
import android.view.View

/**
 * The pointer Curslink draws (Android shows its own only for a mouse that's
 * really plugged into the phone), plus the "pop" and "blink" as it arrives.
 * Fills the screen and lets every touch through.
 */
class PointerView(context: Context) : View(context) {
    enum class Shape { ARROW, TEXT, HAND }

    private val d = resources.displayMetrics.density
    var px = 0f
        private set
    var py = 0f
        private set
    var shape = Shape.ARROW
        set(value) {
            if (field != value) { field = value; invalidate() }
        }
    var shown = false
        set(value) {
            if (field != value) { field = value; invalidate() }
        }
    private var ring = 0f   // 0..1 while popping
    private var blinkAlpha = 1f

    private val fill = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.BLACK; style = Paint.Style.FILL }
    private val edge = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE; style = Paint.Style.STROKE; strokeWidth = 1.5f * d; strokeJoin = Paint.Join.ROUND
    }
    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.rgb(61, 99, 221); style = Paint.Style.STROKE }

    // Android's standard arrow (about 13 x 20 dp), tip at 0, 0.
    private val arrow = Path().apply {
        moveTo(0f, 0f); lineTo(0f, 17f * d); lineTo(4.2f * d, 13.2f * d); lineTo(7.2f * d, 19.8f * d)
        lineTo(9.8f * d, 18.6f * d); lineTo(6.9f * d, 12.2f * d); lineTo(12.4f * d, 12.2f * d); close()
    }

    fun moveTo(x: Float, y: Float) {
        px = x; py = y
        invalidate()
    }

    fun pop() {
        ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 380
            addUpdateListener { ring = it.animatedValue as Float; invalidate() }
            addListener(object : android.animation.AnimatorListenerAdapter() {
                override fun onAnimationEnd(a: android.animation.Animator) { ring = 0f; invalidate() }
            })
            start()
        }
    }

    fun blink() {
        ValueAnimator.ofFloat(0f, 3f).apply {
            duration = 450
            addUpdateListener {
                val t = (it.animatedValue as Float) % 1f
                blinkAlpha = if (t < 0.5f) 1f - t * 2f else (t - 0.5f) * 2f
                invalidate()
            }
            addListener(object : android.animation.AnimatorListenerAdapter() {
                override fun onAnimationEnd(a: android.animation.Animator) { blinkAlpha = 1f; invalidate() }
            })
            start()
        }
    }

    override fun onDraw(canvas: Canvas) {
        if (ring > 0f) {
            ringPaint.alpha = ((1f - ring) * 255).toInt()
            ringPaint.strokeWidth = 4f * d * (1f - ring) + 1f
            canvas.drawCircle(px, py, 8f * d + 40f * d * ring, ringPaint)
        }
        if (!shown) return
        fill.alpha = (blinkAlpha * 255).toInt()
        edge.alpha = fill.alpha
        canvas.save()
        canvas.translate(px, py)
        when (shape) {
            Shape.ARROW -> {
                canvas.drawPath(arrow, fill)
                canvas.drawPath(arrow, edge)
            }
            Shape.TEXT -> drawIBeam(canvas)
            Shape.HAND -> drawHand(canvas)
        }
        canvas.restore()
    }

    private fun drawIBeam(c: Canvas) {
        val h = 9f * d
        val w = 3f * d
        val p = Path().apply {
            moveTo(-w, -h); lineTo(w, -h)
            moveTo(0f, -h); lineTo(0f, h)
            moveTo(-w, h); lineTo(w, h)
        }
        val outline = Paint(edge).apply { strokeWidth = 3.5f * d; strokeCap = Paint.Cap.ROUND }
        val line = Paint(fill).apply { style = Paint.Style.STROKE; strokeWidth = 1.5f * d; strokeCap = Paint.Cap.ROUND }
        c.drawPath(p, outline)
        c.drawPath(p, line)
    }

    // A pointing hand, fingertip at 0, 0.
    private fun drawHand(c: Canvas) {
        val p = Path()
        fun r(l: Float, t: Float, rt: Float, b: Float, rad: Float) =
            p.addRoundRect(RectF(l * d, t * d, rt * d, b * d), rad * d, rad * d, Path.Direction.CW)
        r(-2.2f, 0f, 2.2f, 12f, 2.2f)        // index finger
        r(2.2f, 5.5f, 6.2f, 13f, 2f)         // middle
        r(6.2f, 6.5f, 10f, 13.5f, 1.9f)      // ring
        r(10f, 8f, 13.4f, 14f, 1.7f)         // little
        r(-2.4f, 10.5f, 13.4f, 20f, 4f)      // palm
        r(-7f, 10f, -1f, 14.5f, 2.2f)        // thumb
        p.fillType = Path.FillType.WINDING
        val merged = Path().apply { op(p, Path.Op.UNION) }
        c.drawPath(merged, Paint(fill).apply { color = Color.WHITE; alpha = fill.alpha })
        c.drawPath(merged, Paint(edge).apply { color = Color.BLACK; alpha = fill.alpha; strokeWidth = 1.3f * d })
    }
}
