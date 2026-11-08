package io.github.orimovo.curslink

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.SystemClock
import io.github.orimovo.curslink.core.MIRROR_MAX_B64
import io.github.orimovo.curslink.core.MirrorCapture
import io.github.orimovo.curslink.core.MirrorSession
import java.io.ByteArrayOutputStream
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Mirror mode with the phone behind: captures the parts of the phone's
 * screen another screen covers on the layout, while the phone is in use,
 * and offers them to the host as JPEGs (see core/Mirror.kt).
 *
 * Android only lets an app see its screen through MediaProjection, which
 * asks first ([MirrorConsentActivity]). The projection stays up while there
 * is something to capture, and stops [IDLE_STOP_MS] after there isn't (the
 * phone shows that its screen is being shared while it's up), so the next
 * time asks again. The link service runs as a media projection foreground
 * service while it's up, as Android requires.
 */
class MirrorCapturer(private val context: Context, private val session: () -> MirrorSession?,
                     private val foreground: (Boolean) -> Unit) {
    companion object {
        private const val TAG = "CurslinkMirror"
        private const val IDLE_STOP_MS = 120_000L
        private const val MAX_SIDE = 1280
        @Volatile var instance: MirrorCapturer? = null
            private set
    }

    private val thread = HandlerThread("curslink-mirror").apply { start() }
    private val handler = Handler(thread.looper)
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var captures: List<MirrorCapture> = emptyList()
    private var projection: MediaProjection? = null
    private var display: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var asked = 0L
    private var failed = false
    private val lastSent = HashMap<String, Long>()
    private val lastHash = HashMap<String, Int>()
    private val quality = HashMap<String, Int>()
    private val stopIdle = Runnable { stopProjection("nothing to capture for a while") }

    init { instance = this }

    /** What the host wants captured now (core's MirrorHandler.state). */
    fun set(wanted: List<MirrorCapture>) {
        captures = wanted
        handler.post {
            lastSent.keys.retainAll(wanted.map { it.id }.toSet())
            if (wanted.isEmpty()) {
                handler.removeCallbacks(stopIdle)
                if (projection != null) handler.postDelayed(stopIdle, IDLE_STOP_MS)
                return@post
            }
            handler.removeCallbacks(stopIdle)
            if (projection == null) ask()
        }
    }

    private fun ask() {
        val now = SystemClock.uptimeMillis()
        if (now - asked < 30_000L) return  // one prompt at a time; don't nag
        asked = now
        AppLog.i(TAG, "asking to share the screen for mirror mode")
        val from: Context = InputService.instance ?: context
        val intent = Intent(context, MirrorConsentActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        main.post { runCatching { from.startActivity(intent) }.onFailure { AppLog.w(TAG, "couldn't ask to share the screen", it) } }
    }

    /** The answer to the prompt: start capturing (or not). */
    fun consent(resultCode: Int, data: Intent?) {
        if (resultCode != Activity.RESULT_OK || data == null) {
            AppLog.i(TAG, "screen sharing for mirror mode was declined")
            return
        }
        handler.post { startProjection(resultCode, data) }
    }

    private fun startProjection(resultCode: Int, data: Intent) {
        if (projection != null) return
        foreground(true)  // Android needs the media projection service type first
        val mpm = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        val p = runCatching { mpm.getMediaProjection(resultCode, data) }.getOrNull()
        if (p == null) {
            AppLog.w(TAG, "couldn't start screen sharing")
            foreground(false)
            return
        }
        p.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { handler.post { stopProjection("stopped by the system") } }
        }, handler)
        val size = Screen.sizePx(context)
        val k = min(1.0, MAX_SIDE.toDouble() / max(size.x, size.y))
        val w = (size.x * k).roundToInt()
        val h = (size.y * k).roundToInt()
        val r = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        r.setOnImageAvailableListener({
            try {
                grab(it)
            } catch (e: Exception) {
                if (!failed) AppLog.w(TAG, "couldn't capture the screen for mirror mode", e)
                failed = true
            }
        }, handler)
        val dpi = context.resources.displayMetrics.densityDpi
        display = p.createVirtualDisplay("curslink-mirror", w, h, dpi, DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
            r.surface, null, handler)
        projection = p
        reader = r
        AppLog.i(TAG, "sharing the screen for mirror mode at ${w}x$h")
    }

    private fun stopProjection(why: String) {
        val p = projection ?: return
        AppLog.i(TAG, "stopped sharing the screen: $why")
        projection = null
        runCatching { display?.release() }
        runCatching { reader?.close() }
        display = null
        reader = null
        runCatching { p.stop() }
        foreground(false)
    }

    private fun grab(r: ImageReader) {
        val image = runCatching { r.acquireLatestImage() }.getOrNull() ?: return
        try {
            val now = SystemClock.uptimeMillis()
            val m = session() ?: return
            val due = captures.filter { c -> now - (lastSent[c.id] ?: 0L) >= 1000L / c.fps && m.ready(c.id) }
            if (due.isEmpty()) return
            val plane = image.planes[0]
            val rowPx = plane.rowStride / plane.pixelStride
            val full = Bitmap.createBitmap(rowPx, image.height, Bitmap.Config.ARGB_8888)
            full.copyPixelsFromBuffer(plane.buffer)
            try {
                for (c in due) send(m, c, full, image.width, image.height, now)
            } finally {
                full.recycle()
            }
        } finally {
            image.close()
        }
    }

    private fun send(m: MirrorSession, c: MirrorCapture, full: Bitmap, w: Int, h: Int, now: Long) {
        val (lw, lh) = Prefs(context).layoutSize ?: Screen.naturalSizeDp(context)
        // The capture is of the screen as it's turned now; the layout is in its units.
        val rect = Rect(
            (c.x / lw * w).roundToInt().coerceIn(0, w - 1), (c.y / lh * h).roundToInt().coerceIn(0, h - 1),
            ((c.x + c.w) / lw * w).roundToInt().coerceIn(1, w), ((c.y + c.h) / lh * h).roundToInt().coerceIn(1, h),
        )
        if (rect.width() <= 0 || rect.height() <= 0) return
        val crop = Bitmap.createBitmap(full, rect.left, rect.top, rect.width(), rect.height())
        try {
            val hash = sample(crop)
            if (hash == lastHash[c.id] && now - (lastSent[c.id] ?: 0L) < 2000L) return
            var q = quality[c.id] ?: 72
            var bmp = crop
            var jpeg = ByteArray(0)
            while (true) {
                val out = ByteArrayOutputStream()
                bmp.compress(Bitmap.CompressFormat.JPEG, q, out)
                jpeg = out.toByteArray()
                if (jpeg.size * 4 / 3 + 4 <= MIRROR_MAX_B64) break
                if (q > 45) q -= 10
                else {
                    val smaller = Bitmap.createScaledBitmap(bmp, max(1, (bmp.width * 0.8).toInt()), max(1, (bmp.height * 0.8).toInt()), true)
                    if (bmp !== crop) bmp.recycle()
                    bmp = smaller
                }
            }
            if (bmp !== crop) bmp.recycle()
            quality[c.id] = if (jpeg.size * 4 / 3 < MIRROR_MAX_B64 * 0.6) minOf(72, q + 4) else q
            lastHash[c.id] = hash
            lastSent[c.id] = now
            m.offer(c.id, jpeg, rect.width(), rect.height())
        } finally {
            crop.recycle()
        }
    }

    /** A cheap fingerprint, to skip frames where nothing changed. */
    private fun sample(b: Bitmap): Int {
        var acc = 17
        val sx = max(1, b.width / 24)
        val sy = max(1, b.height / 16)
        var y = 0
        while (y < b.height) {
            var x = 0
            while (x < b.width) { acc = acc * 31 + b.getPixel(x, y); x += sx }
            y += sy
        }
        return acc
    }

    fun close() {
        handler.post { stopProjection("Curslink stopped") }
        thread.quitSafely()
        instance = null
    }
}

/** Asks Android to let Curslink see the screen (MediaProjection's own prompt). */
class MirrorConsentActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val mpm = getSystemService(MEDIA_PROJECTION_SERVICE) as MediaProjectionManager
        @Suppress("DEPRECATION")
        startActivityForResult(mpm.createScreenCaptureIntent(), 1)
    }

    @Deprecated("startActivityForResult is enough for one system prompt")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        MirrorCapturer.instance?.consent(resultCode, data)
        finish()
    }
}
