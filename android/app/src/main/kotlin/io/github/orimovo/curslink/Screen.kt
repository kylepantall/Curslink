package io.github.orimovo.curslink

import android.content.Context
import android.graphics.Point
import android.hardware.display.DisplayManager
import android.os.Build
import android.view.Display
import android.view.Surface
import android.view.WindowManager
import kotlin.math.roundToInt

/** The phone's screen, in the sizes Curslink deals in. */
object Screen {
    private fun display(context: Context): Display =
        (context.getSystemService(Context.DISPLAY_SERVICE) as DisplayManager).getDisplay(Display.DEFAULT_DISPLAY)

    /** The whole screen in pixels, as it's turned now. */
    fun sizePx(context: Context): Point {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        return if (Build.VERSION.SDK_INT >= 30) {
            val b = wm.maximumWindowMetrics.bounds
            Point(b.width(), b.height())
        } else {
            Point().also { @Suppress("DEPRECATION") display(context).getRealSize(it) }
        }
    }

    /**
     * The screen in dp, upright (portrait on a phone): what the computer lays
     * the phone out with. A dp is about a Mac's point, so the pointer moves
     * at a speed that feels the same on both.
     */
    fun naturalSizeDp(context: Context): Pair<Int, Int> {
        val px = sizePx(context)
        val density = context.resources.displayMetrics.density
        val w = (px.x / density).roundToInt()
        val h = (px.y / density).roundToInt()
        val turned = display(context).rotation.let { it == Surface.ROTATION_90 || it == Surface.ROTATION_270 }
        return if (turned) h to w else w to h
    }
}
