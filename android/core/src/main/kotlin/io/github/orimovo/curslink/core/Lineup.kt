package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.intOrNull
import java.util.Base64

/** Listed in hello.features: this phone's camera does the looking for an auto line-up. */
const val LINEUP_CAMERA_FEATURE = "lineup_camera"
/** Listed in welcome.features by a host that lines screens up with a camera. */
const val LINEUP_HOST_FEATURE = "lineup"

/**
 * How a live line-up is going, from the host (`lineup_live`): [seq] is the
 * frame it just read (send the next), [screens] each screen's number and
 * state ("unseen", "seen", "placing" or "locked"), [done] that it's lined up.
 * [buzz]: shake the phone (it's held against a face for the last step, "where
 * you sit", so its screen can't be seen: the buzz says it's been found).
 */
data class LineupStatus(
    val id: String,
    val seq: Int?,
    val progress: Double,
    val hint: String,
    val screens: List<Pair<Int, String>>,
    val done: Boolean,
    val buzz: Boolean = false,
)

/**
 * Auto line-up, like curslink/lineup.py: every screen shows its patterns and
 * the host asks us (`lineup_camera {id, live?}`) to point the camera at the
 * desk.
 *
 * Live: we send frame after frame as the phone is moved about, each as
 * `lineup_photo {id, size, seq, focal?}` and `lineup_data {id, data, end?}`
 * frames (base64, 32 KiB at a time); the host answers each with `lineup_live`
 * (see [LineupStatus]), which is when the next one goes. `lineup_photo {id,
 * done}` says that'll do.
 *
 * Not live (an older host): one photo, sent the same way without a seq.
 * `lineup_photo {id, cancel}` backs out of either. `lineup_request {}` asks
 * the host to start a line-up from the phone.
 */
class LineupSession(
    /** The host wants the camera for line-up [id] ([live]: frame after frame), or, [cancel], doesn't any more. Called on the link's thread. */
    private val onCamera: (id: String, cancel: Boolean, live: Boolean) -> Unit,
) {
    /** Sends a frame to the host while linked (blocking; not on the main thread). */
    @Volatile var send: (String, Map<String, JsonElement>) -> Unit = { _, _ -> }
    /** The host's welcome.features: whether it takes a lineup_request. */
    @Volatile var hostLines = false
    /** The line-up the host is waiting on us for. */
    @Volatile var waiting: String? = null
        private set
    /** How the live line-up is going (called on the link's thread). */
    @Volatile var onStatus: (LineupStatus) -> Unit = {}

    fun handle(f: Frame) {
        when (f.t) {
            "lineup_camera" -> {
                val id = f.str("id")?.takeIf { it.isNotEmpty() } ?: return
                val cancel = f.bool("cancel") == true
                if (cancel) {
                    if (waiting != id) return
                    waiting = null
                } else {
                    waiting = id
                }
                onCamera(id, cancel, f.bool("live") == true)
            }
            "lineup_live" -> {
                val id = f.str("id")?.takeIf { it.isNotEmpty() } ?: return
                val screens = (f["screens"] as? JsonArray).orEmpty().mapNotNull { entry ->
                    val pair = entry as? JsonArray ?: return@mapNotNull null
                    val number = (pair.getOrNull(0) as? JsonPrimitive)?.intOrNull ?: return@mapNotNull null
                    val state = pair.getOrNull(1)?.strOrNull() ?: return@mapNotNull null
                    number to state
                }
                val done = f.bool("done") == true
                if (done && waiting == id) waiting = null
                onStatus(LineupStatus(id, f.num("seq")?.toInt(), (f.num("progress") ?: 0.0).coerceIn(0.0, 1.0),
                    f.str("hint") ?: "", screens, done, f.bool("buzz") == true))
            }
        }
    }

    /** Ask the host to line the screens up (it puts the patterns up and asks us for the camera). Blocks. */
    fun request(): Boolean {
        if (!hostLines) return false
        send("lineup_request", emptyMap())
        return true
    }

    /** The one photo for line-up [id] (not live): send it (JPEG bytes). Blocks. */
    fun photo(id: String, jpeg: ByteArray) {
        bytes(id, jpeg, jsonOf("id" to id, "size" to jpeg.size))
        if (waiting == id) waiting = null
    }

    /**
     * Frame number [seq] of live line-up [id] (JPEG bytes); [focal] is the
     * camera's focal length in the frame's pixels, where known. Blocks.
     */
    fun frame(id: String, seq: Int, jpeg: ByteArray, focal: Double?) {
        bytes(id, jpeg, jsonOf("id" to id, "size" to jpeg.size, "seq" to seq, "focal" to focal))
    }

    private fun bytes(id: String, jpeg: ByteArray, first: Map<String, JsonElement>) {
        val out = send
        out("lineup_photo", first)
        var i = 0
        while (true) {
            val end = minOf(jpeg.size, i + CHUNK)
            val last = end >= jpeg.size
            val data = Base64.getEncoder().encodeToString(jpeg.copyOfRange(i, end))
            out("lineup_data", jsonOf("id" to id, "data" to data, "end" to if (last) true else null))
            if (last) break
            i = end
        }
    }

    /** The person says the live line-up will do as it is. Blocks. */
    fun done(id: String) {
        send("lineup_photo", jsonOf("id" to id, "done" to true))
    }

    /** The person backed out. Blocks. */
    fun cancel(id: String) {
        if (waiting == id) waiting = null
        send("lineup_photo", jsonOf("id" to id, "cancel" to true))
    }

    fun reset() {
        send = { _, _ -> }
        hostLines = false
        waiting = null
    }

    companion object {
        const val CHUNK = 32 * 1024
    }
}
