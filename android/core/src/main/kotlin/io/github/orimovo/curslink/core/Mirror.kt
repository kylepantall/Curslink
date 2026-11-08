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

/** A part of this phone's screen to show another screen's covered part on. */
data class MirrorShow(
    val id: String, val x: Double, val y: Double, val w: Double, val h: Double,
    val label: String, val edges: Set<String>,
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

    private fun rect(o: JsonObject): List<Double>? =
        (o["os"] as? JsonArray)?.mapNotNull { it.numOrNull() }?.takeIf { it.size == 4 && it[2] > 0 && it[3] > 0 }

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
        return MirrorShow(id, r[0], r[1], r[2], r[3], o["label"]?.strOrNull() ?: "", edges)
    }
}
