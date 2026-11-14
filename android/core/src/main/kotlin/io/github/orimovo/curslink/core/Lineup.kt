package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import java.util.Base64

/** Listed in hello.features: this phone takes the photo for an auto line-up. */
const val LINEUP_CAMERA_FEATURE = "lineup_camera"
/** Listed in welcome.features by a host that lines screens up from a photo. */
const val LINEUP_HOST_FEATURE = "lineup"

/**
 * Auto line-up, like curslink/lineup.py: every screen shows its patterns,
 * the host asks us (`lineup_camera {id}`) for a photo of the desk, and we
 * send it back as `lineup_photo {id, size}` and `lineup_data {id, data,
 * end?}` frames (base64, 32 KiB at a time), or `lineup_photo {id, cancel}`.
 * `lineup_request {}` asks the host to start one from the phone.
 */
class LineupSession(
    /** The host wants a photo for line-up [id] (or, [cancel], doesn't any more). Called on the link's thread. */
    private val onCamera: (id: String, cancel: Boolean) -> Unit,
) {
    /** Sends a frame to the host while linked (blocking; not on the main thread). */
    @Volatile var send: (String, Map<String, JsonElement>) -> Unit = { _, _ -> }
    /** The host's welcome.features: whether it takes a lineup_request. */
    @Volatile var hostLines = false
    /** The line-up the host is waiting on a photo for. */
    @Volatile var waiting: String? = null
        private set

    fun handle(f: Frame) {
        if (f.t != "lineup_camera") return
        val id = f.str("id")?.takeIf { it.isNotEmpty() } ?: return
        val cancel = f.bool("cancel") == true
        if (cancel) {
            if (waiting != id) return
            waiting = null
        } else {
            waiting = id
        }
        onCamera(id, cancel)
    }

    /** Ask the host to line the screens up (it puts the patterns up and asks us for a photo). Blocks. */
    fun request(): Boolean {
        if (!hostLines) return false
        send("lineup_request", emptyMap())
        return true
    }

    /** The photo for line-up [id]: send it (JPEG bytes). Blocks. */
    fun photo(id: String, jpeg: ByteArray) {
        val out = send
        out("lineup_photo", jsonOf("id" to id, "size" to jpeg.size))
        var i = 0
        while (true) {
            val end = minOf(jpeg.size, i + CHUNK)
            val last = end >= jpeg.size
            val data = Base64.getEncoder().encodeToString(jpeg.copyOfRange(i, end))
            out("lineup_data", jsonOf("id" to id, "data" to data, "end" to if (last) true else null))
            if (last) break
            i = end
        }
        if (waiting == id) waiting = null
    }

    /** The person backed out of the photo. Blocks. */
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
