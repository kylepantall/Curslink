package io.github.orimovo.curslink

import android.content.Context
import android.hardware.display.DisplayManager
import android.os.Build
import android.util.DisplayMetrics
import android.view.Display
import kotlin.math.roundToInt

/**
 * Samsung DeX as a screen of its own. With DeX on (on a monitor or TV by
 * cable, or Wireless DeX to a TV, a monitor, or a Windows PC's Wireless
 * Display app) the phone drives a second display with a desktop on it. We
 * report that display to the computer as a second screen, "dex", beside
 * the phone's own in the phone's coordinates, so it goes on the Layout page
 * like any monitor; input that lands there is played on that display
 * (InputService), and the pointer is drawn there.
 *
 * Needs Android 11 (gestures and overlays on another display). Plain screen
 * mirroring (Smart View) isn't DeX: its display shows the phone's own
 * screen, so it isn't reported.
 */
object Dex {
    private const val TAG = "CurslinkDex"
    /** Too small to be a desktop: not DeX (a cover screen, a stray virtual display). */
    private const val MIN_DP = 480

    /** The DeX display, in the units the computer lays it out in. */
    data class Screen(
        val displayId: Int,
        /** Its size in dp (what the host's pointer moves in), and in pixels. */
        val widthDp: Int, val heightDp: Int,
        val widthPx: Int, val heightPx: Int,
        val name: String,
    )

    @Volatile private var cached: Screen? = null
    @Volatile private var looked = false
    private var lastSaid = ""

    /** The DeX display now (null: DeX is off, or this phone can't do it). */
    fun current(context: Context): Screen? {
        if (!looked) refresh(context)
        return cached
    }

    /** Look again (a display came, went or changed). True if that changed anything. */
    fun refresh(context: Context): Boolean {
        val found = runCatching { find(context) }.onFailure { AppLog.w(TAG, "couldn't look for DeX", it) }.getOrNull()
        looked = true
        if (found == cached) return false
        cached = found
        return true
    }

    private fun find(context: Context): Screen? {
        if (Build.VERSION.SDK_INT < 30) return null
        val dm = context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager
        val samsung = Build.MANUFACTURER.equals("samsung", ignoreCase = true)
        val notes = mutableListOf<String>()
        var best: Screen? = null
        for (d in dm.displays) {
            if (d.displayId == Display.DEFAULT_DISPLAY) continue
            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION") d.getRealMetrics(metrics)
            val density = metrics.densityDpi.takeIf { it > 0 }?.let { it / 160f } ?: 1f
            val wDp = (metrics.widthPixels / density).roundToInt()
            val hDp = (metrics.heightPixels / density).roundToInt()
            val isPrivate = (d.flags and Display.FLAG_PRIVATE) != 0
            val desktop = desktopMode(context, d)
            notes += "${d.displayId} \"${d.name}\" ${metrics.widthPixels}x${metrics.heightPixels} (${wDp}x${hDp} dp), " +
                "state ${d.state}, flags ${d.flags}, desktop mode ${desktop ?: "?"}"
            if (isPrivate || d.state == Display.STATE_OFF) continue
            if (maxOf(wDp, hDp) < MIN_DP) continue
            // Samsung says whether DeX runs there; elsewhere, nothing to go by.
            if (desktop == false || (desktop == null && !samsung)) continue
            if (best == null || wDp * hDp > best.widthDp * best.heightDp) {
                best = Screen(d.displayId, wDp, hDp, metrics.widthPixels, metrics.heightPixels, d.name ?: "DeX")
            }
        }
        val said = if (best != null) "Samsung DeX is on: display ${best.displayId}, ${best.widthDp}x${best.heightDp} dp"
            else if (notes.isEmpty()) "no DeX (no other display)" else "no DeX on the other displays: ${notes.joinToString("; ")}"
        if (said != lastSaid) {
            lastSaid = said
            AppLog.i(TAG, said + if (best != null && notes.size > 1) " (of ${notes.joinToString("; ")})" else "")
        }
        return best
    }

    /**
     * Samsung's own word on whether a display runs DeX: its configuration's
     * semDesktopModeEnabled (1 on, 0 off). Null when there's no such thing
     * (not a Samsung, or it moved).
     */
    private fun desktopMode(context: Context, d: Display): Boolean? = runCatching {
        val config = context.createDisplayContext(d).resources.configuration
        val field = config.javaClass.getField("semDesktopModeEnabled")
        field.getInt(config) == 1
    }.getOrNull()

    /** The screen we report for it: beside the phone's ([phoneWidth] dp wide), so their coordinates never overlap. */
    fun report(s: Screen, phoneWidth: Int): Map<String, Any> = mapOf(
        "id" to "dex", "name" to "Samsung DeX",
        "x" to phoneWidth, "y" to 0, "width" to s.widthDp, "height" to s.heightDp,
    )
}
