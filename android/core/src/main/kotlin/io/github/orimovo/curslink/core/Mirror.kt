package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import java.util.Base64

/**
 * Mirror mode (curslink/mirror on the computer): where screens overlap on the
 * layout, the screen in front shows the part of the screen in use it covers.
 * The host says what this phone should capture and show (`mirror`), sends
 * frames to show (`mirror_frame`, answered `mirror_ack`) and where the
 * pointer is in them (`mirror_pointer`), and passes on answers to frames we
 * captured (`mirror_ack`).
 */

/** A part of this phone's screen to capture, in the layout's (dp) units. */
data class MirrorCapture(val id: String, val x: Double, val y: Double, val w: Double, val h: Double, val fps: Int)

/**
 * A part of this phone's screen to show another screen's covered part on.
 * While it's being lined up ([tune]) it takes the pointer (see [MirrorTuner])
 * and draws alignment guides: [guides] is where they're drawn on the screen
 * behind and [src] what this mirror shows of it, both in that screen's OS
 * pixels.
 */
data class MirrorShow(
    val id: String, val x: Double, val y: Double, val w: Double, val h: Double,
    val label: String, val edges: Set<String>,
    val tune: Boolean = false, val guides: List<Double>? = null, val src: List<Double>? = null,
)

/** What the app does about it. Called on the link's thread. */
interface MirrorHandler {
    fun state(captures: List<MirrorCapture>, shows: List<MirrorShow>)
    fun image(id: String, jpeg: ByteArray)
    /** Where the pointer is in a mirror, 0..1 across and down, or null for none. */
    fun pointer(id: String, x: Double?, y: Double?)
}

/** A frame's base64 JPEG must fit one protocol frame, with room to spare. */
const val MIRROR_MAX_B64 = 60_000
/** Frames a capture may have on their way before one is shown. */
const val MIRROR_IN_FLIGHT = 2

class MirrorSession(private val handler: MirrorHandler) {
    /** Sends a frame to the host (set by AgentLink while connected). */
    @Volatile var send: (String, Map<String, JsonElement>) -> Unit = { _, _ -> }
    private val flying = HashMap<String, Int>()
    private val waiting = HashMap<String, Map<String, JsonElement>>()
    private var showing = emptySet<String>()

    /** A reply to send back, if the frame needs one. */
    fun handle(f: Frame): Pair<String, Map<String, JsonElement>>? {
        when (f.t) {
            "mirror" -> {
                val captures = (f["captures"] as? JsonArray).orEmpty().mapNotNull { capture(it as? JsonObject) }
                val shows = (f["shows"] as? JsonArray).orEmpty().mapNotNull { show(it as? JsonObject) }
                synchronized(this) {
                    val ids = captures.map { it.id }.toSet()
                    flying.keys.retainAll(ids)
                    waiting.keys.retainAll(ids)
                    ids.forEach { flying.putIfAbsent(it, 0) }
                    showing = shows.map { it.id }.toSet()
                }
                handler.state(captures, shows)
            }
            "mirror_frame" -> {
                val id = f.str("id") ?: return null
                val jpeg = f.str("jpg")?.let { runCatching { Base64.getDecoder().decode(it) }.getOrNull() }
                if (jpeg != null && synchronized(this) { id in showing }) handler.image(id, jpeg)
                return "mirror_ack" to jsonOf("id" to id)  // answered even if dropped, so the capture never stalls
            }
            "mirror_pointer" -> {
                val id = f.str("id") ?: return null
                if (f.bool("hide") == true) handler.pointer(id, null, null)
                else handler.pointer(id, f.num("x") ?: return null, f.num("y") ?: return null)
            }
            "mirror_ack" -> ack(f.str("id") ?: return null)
        }
        return null
    }

    /** A captured frame (JPEG) for capture [id]: goes now, or waits for an answer. */
    fun offer(id: String, jpeg: ByteArray, w: Int, h: Int) {
        val data = jsonOf("id" to id, "jpg" to Base64.getEncoder().encodeToString(jpeg), "w" to w, "h" to h)
        val now = synchronized(this) {
            val n = flying[id] ?: return
            if (n >= MIRROR_IN_FLIGHT) { waiting[id] = data; false } else { flying[id] = n + 1; true }
        }
        if (now) runCatching { send("mirror_frame", data) }
    }

    /** Lining up mirror [id]: a drag, stretch or zoom of it (see [MirrorTuner]) for the host. */
    // Called from the phone's main thread, where Android won't let a socket
    // be written (NetworkOnMainThreadException): sent from a thread of its own.
    fun tune(id: String, data: Map<String, Any?>) {
        val frame = jsonOf("id" to id, *data.toList().toTypedArray())
        tuneOut.execute { runCatching { send("mirror_tune", frame) } }
    }

    private val tuneOut = java.util.concurrent.Executors.newSingleThreadExecutor { r ->
        Thread(r, "curslink-mirror-tune").apply { isDaemon = true }
    }

    /** Is capture [id] free to send a frame now? (Saves encoding one that would wait.) */
    fun ready(id: String): Boolean = synchronized(this) { (flying[id] ?: return false) < MIRROR_IN_FLIGHT }

    private fun ack(id: String) {
        val next = synchronized(this) {
            val n = flying[id] ?: return
            flying[id] = maxOf(0, n - 1)
            waiting.remove(id)?.also { flying[id] = flying[id]!! + 1 }
        }
        if (next != null) runCatching { send("mirror_frame", next) }
    }

    /** The link went down: nothing to capture or show until the host says again. */
    fun reset() {
        val had = synchronized(this) {
            (flying.isNotEmpty() || showing.isNotEmpty()).also { flying.clear(); waiting.clear(); showing = emptySet() }
        }
        if (had) handler.state(emptyList(), emptyList())
    }

    private fun rect(o: JsonObject, key: String = "os"): List<Double>? =
        (o[key] as? JsonArray)?.mapNotNull { it.numOrNull() }?.takeIf { it.size == 4 && it[2] > 0 && it[3] > 0 }

    private fun capture(o: JsonObject?): MirrorCapture? {
        o ?: return null
        val id = o["id"]?.strOrNull() ?: return null
        val r = rect(o) ?: return null
        val fps = (o["fps"].numOrNull() ?: 10.0).toInt().coerceIn(1, 30)
        return MirrorCapture(id, r[0], r[1], r[2], r[3], fps)
    }

    private fun show(o: JsonObject?): MirrorShow? {
        o ?: return null
        val id = o["id"]?.strOrNull() ?: return null
        val r = rect(o) ?: return null
        val edges = (o["edges"] as? JsonArray).orEmpty().mapNotNull { it.strOrNull() }.toSet()
        val tune = (o["tune"] as? kotlinx.serialization.json.JsonPrimitive)?.takeIf { !it.isString }?.content == "true"
        return MirrorShow(id, r[0], r[1], r[2], r[3], o["label"]?.strOrNull() ?: "", edges,
            tune, if (tune) rect(o, "guides") else null, if (tune) rect(o, "src") else null)
    }
}

/**
 * Lining up a mirror on the phone, as the computer's mirror window does
 * (curslink/mirror/helper.py): pressing inside it and dragging moves it,
 * pressing within [grab] of a side and dragging stretches it about the
 * opposite side, and the wheel zooms about the pointer. Each step is what to
 * send as `mirror_tune` (without the id). Rectangles and points in pixels.
 */
class MirrorTuner(private val grab: Double) {
    private var drag: String? = null
    private var lastX = 0.0
    private var lastY = 0.0
    val dragging: Boolean get() = drag != null

    /** Sides of a mirror [x0, y0, w, h] near (px, py): some of "l", "r", "t", "b". */
    fun sides(r: List<Double>, px: Double, py: Double): String {
        var out = ""
        if (px < r[0] + grab) out += "l" else if (px > r[0] + r[2] - grab) out += "r"
        if (py < r[1] + grab) out += "t" else if (py > r[1] + r[3] - grab) out += "b"
        return out
    }

    /** The button went down at (px, py): true if it's on the mirror (it takes the press). */
    fun press(r: List<Double>, px: Double, py: Double): Boolean {
        if (px < r[0] || py < r[1] || px > r[0] + r[2] || py > r[1] + r[3]) return false
        drag = sides(r, px, py).ifEmpty { "move" }
        lastX = px; lastY = py
        return true
    }

    fun move(r: List<Double>, px: Double, py: Double): Map<String, Any?>? {
        val which = drag ?: return null
        val dx = px - lastX
        val dy = py - lastY
        if (kotlin.math.abs(dx) < 1 && kotlin.math.abs(dy) < 1) return null
        lastX = px; lastY = py
        val w = maxOf(1.0, r[2])
        val h = maxOf(1.0, r[3])
        if (which == "move") return mapOf("mx" to round(dx / w, 5), "my" to round(dy / h, 5))
        val factors = mutableListOf<Double>()
        var ax = 0.5
        var ay = 0.5
        if ('r' in which) { factors += (w + dx) / w; ax = 0.0 }
        if ('l' in which) { factors += (w - dx) / w; ax = 1.0 }
        if ('b' in which) { factors += (h + dy) / h; ay = 0.0 }
        if ('t' in which) { factors += (h - dy) / h; ay = 1.0 }
        val factor = factors.average()
        return if (factor > 0) mapOf("grow" to round(factor, 5), "ax" to ax, "ay" to ay) else null
    }

    fun release() { drag = null }

    // Fingers on the phone's own screen: their middle, how far apart they are, how many.
    private var fingers = 0
    private var fx = 0.0
    private var fy = 0.0
    private var span = 0.0

    /**
     * The fingers touching the mirror [r] are at ([xs], [ys]): one moves it,
     * two also zoom it about their middle as they spread or pinch. A change
     * in how many fingers starts again from where they are.
     */
    fun touch(r: List<Double>, xs: DoubleArray, ys: DoubleArray): List<Map<String, Any?>> {
        val n = minOf(xs.size, ys.size)
        if (n == 0) { fingers = 0; return emptyList() }
        val cx = xs.take(n).average()
        val cy = ys.take(n).average()
        val sp = if (n < 2) 0.0 else (0 until n).map { kotlin.math.hypot(xs[it] - cx, ys[it] - cy) }.average()
        if (n != fingers) {
            fingers = n; fx = cx; fy = cy; span = sp
            return emptyList()
        }
        val out = mutableListOf<Map<String, Any?>>()
        val w = maxOf(1.0, r[2])
        val h = maxOf(1.0, r[3])
        if (n >= 2 && span > 0 && sp > 0 && kotlin.math.abs(sp - span) >= 1) {
            out += mapOf("grow" to round(sp / span, 5), "ax" to round((cx - r[0]) / w, 4), "ay" to round((cy - r[1]) / h, 4))
            span = sp
        }
        val dx = cx - fx
        val dy = cy - fy
        if (kotlin.math.abs(dx) >= 1 || kotlin.math.abs(dy) >= 1) {
            out += mapOf("mx" to round(dx / w, 5), "my" to round(dy / h, 5))
            fx = cx; fy = cy
        }
        return out
    }

    fun touchEnd() { fingers = 0 }

    /** The wheel turned [steps] notches (up is positive) with the pointer at (px, py). */
    fun wheel(r: List<Double>, px: Double, py: Double, steps: Double): Map<String, Any?>? {
        if (steps == 0.0 || px < r[0] || py < r[1] || px > r[0] + r[2] || py > r[1] + r[3]) return null
        return mapOf("grow" to round(Math.pow(1.05, steps), 5),
            "ax" to round((px - r[0]) / maxOf(1.0, r[2]), 4), "ay" to round((py - r[1]) / maxOf(1.0, r[3]), 4))
    }

    private fun round(v: Double, places: Int): Double {
        val k = Math.pow(10.0, places.toDouble())
        return Math.round(v * k) / k
    }
}
