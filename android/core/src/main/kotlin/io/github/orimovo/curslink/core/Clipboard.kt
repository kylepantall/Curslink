package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import java.io.ByteArrayOutputStream
import java.util.Base64
import java.util.UUID

/** Listed in hello.features: this agent shares copied text (only text: no pictures or files). */
const val CLIPBOARD_TEXT_FEATURE = "clipboard_text"

/**
 * Clipboard sync, text only, like curslink/clipboard/sync.py: the host
 * sends what was copied on a computer as `clip {id, kind, size}` and then
 * `clip_data {id, data, end?}` frames (base64 of the UTF-8, 32 KiB at a
 * time); we send what was copied here the same way. A newer clip replaces
 * one still arriving.
 */
class ClipboardSession(
    /** Text copied on another device: put it on this one's clipboard. Called on the link's thread. */
    private val onText: (String) -> Unit,
    private val maxBytes: Int = 1 shl 20,
) {
    /** Sends a frame to the host while linked (blocking; not on the main thread). */
    @Volatile var send: (String, Map<String, JsonElement>) -> Unit = { _, _ -> }
    private var id: String? = null
    private var size = 0
    private val buf = ByteArrayOutputStream()

    fun handle(f: Frame) {
        val text = synchronized(this) { take(f) } ?: return
        onText(text)
    }

    private fun take(f: Frame): String? {
        when (f.t) {
            "clip" -> {
                id = null
                buf.reset()
                val cid = f.str("id") ?: return null
                val n = f.num("size")?.toInt() ?: return null
                if (f.str("kind") != "text" || n < 0 || n > maxBytes) return null
                id = cid
                size = n
            }
            "clip_data" -> {
                if (id == null || f.str("id") != id) return null
                val bytes = try {
                    Base64.getDecoder().decode(f.str("data") ?: "")
                } catch (e: IllegalArgumentException) {
                    id = null
                    return null
                }
                if (buf.size() + bytes.size > size) {
                    id = null
                    return null
                }
                buf.write(bytes)
                if (f.bool("end") == true) {
                    id = null
                    if (buf.size() == size) return buf.toString(Charsets.UTF_8.name())
                }
            }
        }
        return null
    }

    /** Text was copied here: send it to the host (and so to every computer). Blocks. */
    fun copied(text: String) {
        val raw = text.toByteArray(Charsets.UTF_8)
        if (raw.isEmpty() || raw.size > maxBytes) return
        val cid = UUID.randomUUID().toString().replace("-", "").take(12)
        val out = send
        out("clip", jsonOf("id" to cid, "kind" to "text", "size" to raw.size))
        var i = 0
        while (true) {
            val end = minOf(raw.size, i + CHUNK)
            val last = end >= raw.size
            val data = Base64.getEncoder().encodeToString(raw.copyOfRange(i, end))
            out("clip_data", jsonOf("id" to cid, "data" to data, "end" to if (last) true else null))
            if (last) return
            i = end
        }
    }

    @Synchronized fun reset() {
        id = null
        buf.reset()
        send = { _, _ -> }
    }

    companion object {
        const val CHUNK = 32 * 1024
    }
}
