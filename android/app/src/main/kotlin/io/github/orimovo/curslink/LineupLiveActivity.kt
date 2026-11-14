package io.github.orimovo.curslink

import android.Manifest
import android.annotation.SuppressLint
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.Typeface
import android.graphics.YuvImage
import android.graphics.drawable.GradientDrawable
import android.hardware.camera2.CameraCharacteristics
import android.os.Bundle
import android.os.SystemClock
import android.text.SpannableStringBuilder
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.util.Size
import android.view.Gravity
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.camera2.interop.Camera2CameraInfo
import androidx.camera.core.Camera
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import io.github.orimovo.curslink.core.LineupStatus
import java.io.ByteArrayOutputStream
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import kotlin.concurrent.thread
import kotlin.math.roundToInt

/**
 * Live auto line-up (see curslink/lineup.py and lineup_scene.py on the
 * computer): every screen shows its patterns, and this streams what the
 * camera sees while the phone is moved in a figure of 8. The computer reads
 * each frame, works out where every screen stands, and says how it's going
 * (which screens are locked, what to do next), which is shown over the
 * camera's picture. It ends by itself when everything is lined up.
 *
 * One frame is in flight at a time: the next goes when the computer has
 * answered the last (or after [ANSWER_TIMEOUT_MS]), so a slow link just
 * means fewer frames, never a queue of stale ones.
 */
class LineupLiveActivity : AppCompatActivity() {
    private var id = ""
    private lateinit var preview: PreviewView
    private lateinit var hint: TextView
    private lateinit var chips: TextView
    private lateinit var bar: ProgressBar
    private lateinit var useButton: Button
    private var analysisThread: ExecutorService? = null
    @Volatile private var seq = 0
    @Volatile private var awaiting = -1  // the frame the computer hasn't answered yet
    @Volatile private var sentAt = 0L
    @Volatile private var over = false
    @Volatile private var focalMm = 0f
    @Volatile private var sensorW = 0f
    @Volatile private var sensorH = 0f
    private var failures = 0

    private val askCamera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startCamera() else giveUp("Curslink needs the camera to see your screens.")
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        id = intent.getStringExtra(LineupActivity.EXTRA_ID) ?: run { finish(); return }
        current = this
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        build()
        LinkService.instance?.lineupStatus { status -> runOnUiThread { show(status) } }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) startCamera()
        else askCamera.launch(Manifest.permission.CAMERA)
    }

    override fun onDestroy() {
        if (current === this) current = null
        over = true
        LinkService.instance?.lineupStatus(null)
        analysisThread?.shutdown()
        super.onDestroy()
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        giveUp(null)
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun panel(radius: Int) = GradientDrawable().apply {
        setColor(Color.argb(200, 8, 12, 20))
        cornerRadius = dp(radius).toFloat()
    }

    private fun build() {
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        preview = PreviewView(this).apply { scaleType = PreviewView.ScaleType.FIT_CENTER }
        root.addView(preview, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT))

        hint = TextView(this).apply {
            text = "Hold the phone up in front of your eyes, pointing at your screens"
            textSize = 17f
            setTextColor(Color.WHITE)
            setTypeface(typeface, Typeface.BOLD)
            background = panel(14)
            setPadding(dp(14), dp(12), dp(14), dp(12))
        }
        root.addView(hint, FrameLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT, Gravity.TOP).apply {
            setMargins(dp(14), dp(40), dp(14), 0)
        })

        val bottom = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = panel(14)
            setPadding(dp(14), dp(12), dp(14), dp(10))
        }
        chips = TextView(this).apply {
            textSize = 15f
            setTextColor(Color.rgb(154, 168, 189))
            text = "Looking for your screens' patterns…"
        }
        bottom.addView(chips)
        bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            max = 1000
            progress = 0
        }
        bottom.addView(bar, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { topMargin = dp(8) })
        val buttons = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        buttons.addView(Button(this).apply {
            text = "Cancel"
            isAllCaps = false
            setOnClickListener { giveUp(null) }
        }, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        useButton = Button(this).apply {
            text = "Use it as it is"
            isAllCaps = false
            isEnabled = false
            setOnClickListener { useNow() }
        }
        buttons.addView(useButton, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        bottom.addView(buttons, LinearLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT).apply { topMargin = dp(6) })
        root.addView(bottom, FrameLayout.LayoutParams(MATCH_PARENT, WRAP_CONTENT, Gravity.BOTTOM).apply {
            setMargins(dp(14), 0, dp(14), dp(28))
        })
        setContentView(root)
    }

    /** What the computer says about the scan so far. */
    private fun show(status: LineupStatus) {
        if (status.id != id || over) return
        status.seq?.let { if (it >= awaiting) awaiting = -1 }  // answered: the next frame can go
        if (status.hint.isNotEmpty()) hint.text = status.hint
        bar.progress = (status.progress * 1000).roundToInt()
        if (status.screens.isNotEmpty()) {
            val line = SpannableStringBuilder()
            var placed = 0
            for ((number, state) in status.screens.sortedBy { it.first }) {
                val (label, colour) = when (state) {
                    "locked" -> "lined up" to LOCKED
                    "placing" -> "measuring" to PLACING
                    "seen" -> "seen" to UNSEEN
                    else -> "not seen" to UNSEEN
                }
                if (state == "locked" || state == "placing") placed++
                if (line.isNotEmpty()) line.append("     ")
                val start = line.length
                line.append("$number  $label")
                line.setSpan(ForegroundColorSpan(colour), start, line.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
            }
            chips.text = line
            useButton.isEnabled = placed >= 2
        }
        if (status.done) {
            over = true
            hint.text = if (status.hint.isNotEmpty()) "${status.hint}. Check the layout on your computer." else "Done. Check the layout on your computer."
            bar.progress = 1000
            preview.postDelayed({ finish() }, 1400)
        }
    }

    private fun startCamera() {
        val future = ProcessCameraProvider.getInstance(this)
        future.addListener({
            val provider = runCatching { future.get() }.getOrElse {
                giveUp("The camera couldn't be opened.")
                return@addListener
            }
            val view = Preview.Builder().build().also { it.setSurfaceProvider(preview.surfaceProvider) }
            // 4:3 is the whole sensor (the widest view, and the focal length works out
            // simply); about 1600 px across is plenty to read the patterns to a fraction of a pixel.
            val analysis = ImageAnalysis.Builder()
                .setResolutionSelector(ResolutionSelector.Builder()
                    .setAspectRatioStrategy(AspectRatioStrategy.RATIO_4_3_FALLBACK_AUTO_STRATEGY)
                    .setResolutionStrategy(ResolutionStrategy(Size(1600, 1200), ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER))
                    .build())
                .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .build()
            val worker = Executors.newSingleThreadExecutor()
            analysisThread = worker
            analysis.setAnalyzer(worker) { image -> try { frame(image) } finally { image.close() } }
            runCatching {
                provider.unbindAll()
                val camera = provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, view, analysis)
                tune(camera)
            }.onFailure {
                AppLog.w(TAG, "couldn't start the camera for the line-up", it)
                giveUp("The camera couldn't be opened.")
            }
        }, ContextCompat.getMainExecutor(this))
    }

    /** The lens, for the computer's sums, and a darker exposure: screens glow, and blown-out white eats into the patterns. */
    @SuppressLint("UnsafeOptInUsageError", "RestrictedApi")
    private fun tune(camera: Camera) {
        runCatching {
            val info = Camera2CameraInfo.from(camera.cameraInfo)
            val focals = info.getCameraCharacteristic(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
            val sensor = info.getCameraCharacteristic(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
            if (focals != null && focals.isNotEmpty() && sensor != null && sensor.width > 0 && sensor.height > 0) {
                focalMm = focals[0]
                sensorW = maxOf(sensor.width, sensor.height)
                sensorH = minOf(sensor.width, sensor.height)
            }
        }.onFailure { AppLog.i(TAG, "the camera doesn't say its focal length: $it") }
        runCatching {
            val exposure = camera.cameraInfo.exposureState
            if (exposure.isExposureCompensationSupported) {
                val step = exposure.exposureCompensationStep.toFloat()
                if (step > 0) {
                    val range = exposure.exposureCompensationRange
                    val index = (-1f / step).roundToInt().coerceIn(range.lower, range.upper)
                    camera.cameraControl.setExposureCompensationIndex(index)
                }
            }
        }
    }

    /** The focal length in pixels of a [w] x [h] frame, or null where the camera doesn't say. */
    private fun focalPx(w: Int, h: Int): Double? {
        if (focalMm <= 0f || sensorW <= 0f || sensorH <= 0f) return null
        val long = maxOf(w, h).toDouble()
        val short = minOf(w, h).toDouble()
        // The frame is the sensor cropped to its shape: full width if it's
        // as wide as the sensor (or wider), else full height.
        return if (long / short >= (sensorW / sensorH) * 0.99) focalMm / sensorW * long else focalMm / sensorH * short
    }

    /** A camera frame (on the analysis thread): send it if the computer is ready for one. */
    private fun frame(image: ImageProxy) {
        if (over) return
        val now = SystemClock.elapsedRealtime()
        if (awaiting >= 0 && now - sentAt < ANSWER_TIMEOUT_MS) return
        if (now - sentAt < MIN_GAP_MS) return
        val service = LinkService.instance ?: return
        val jpeg = runCatching { grayJpeg(image) }.getOrElse {
            AppLog.w(TAG, "couldn't read a camera frame", it)
            return
        }
        val n = seq++
        awaiting = n
        sentAt = now
        runCatching { service.lineupFrame(id, n, jpeg, focalPx(image.width, image.height)) }
            .onSuccess { failures = 0 }
            .onFailure {
                awaiting = -1
                if (++failures >= 5 && !over) {
                    AppLog.w(TAG, "the line-up's frames aren't getting to the computer", it)
                    runOnUiThread { giveUp("Lost the link to the computer.") }
                }
            }
    }

    private fun useNow() {
        if (over) return
        useButton.isEnabled = false
        hint.text = "Stopping…"
        val lineupId = id
        thread(name = "curslink-lineup") { runCatching { LinkService.instance?.lineupDone(lineupId) } }
        // The computer answers with done (or says it hasn't enough yet, and the scan goes on).
        preview.postDelayed({ if (!over) useButton.isEnabled = true }, 2000)
    }

    private fun giveUp(why: String?) {
        if (why != null) Toast.makeText(this, why, Toast.LENGTH_LONG).show()
        if (!over) {
            over = true
            val lineupId = id
            thread(name = "curslink-lineup") { runCatching { LinkService.instance?.lineupCancel(lineupId) } }
        }
        finish()
    }

    companion object {
        private const val TAG = "CurslinkLineup"
        private const val ANSWER_TIMEOUT_MS = 2500L
        private const val MIN_GAP_MS = 120L
        private const val JPEG_QUALITY = 82
        private val LOCKED = Color.rgb(79, 214, 164)
        private val PLACING = Color.rgb(255, 180, 84)
        private val UNSEEN = Color.rgb(154, 168, 189)
        @Volatile private var current: LineupLiveActivity? = null

        /** The computer doesn't need line-up [id]'s camera any more. */
        fun close(id: String) {
            current?.let { a -> a.runOnUiThread { if (a.id == id && !a.over) { a.over = true; a.finish() } } }
        }

        /**
         * The frame's brightness alone, as a JPEG: the patterns are black and
         * white, and grey is a third of the bytes to send.
         */
        fun grayJpeg(image: ImageProxy): ByteArray {
            val w = image.width
            val h = image.height
            val plane = image.planes[0]
            val buffer = plane.buffer
            val stride = plane.rowStride
            // NV21: the brightness, then colour at half size, here all neutral (128).
            val nv21 = ByteArray(w * h + 2 * ((w + 1) / 2) * ((h + 1) / 2))
            if (stride == w) {
                buffer.position(0)
                buffer.get(nv21, 0, minOf(w * h, buffer.remaining()))
            } else {
                for (row in 0 until h) {
                    buffer.position(row * stride)
                    buffer.get(nv21, row * w, minOf(w, buffer.remaining()))
                }
            }
            java.util.Arrays.fill(nv21, w * h, nv21.size, 128.toByte())
            val out = ByteArrayOutputStream(256 * 1024)
            YuvImage(nv21, ImageFormat.NV21, w, h, null).compressToJpeg(Rect(0, 0, w, h), JPEG_QUALITY, out)
            return out.toByteArray()
        }
    }
}
