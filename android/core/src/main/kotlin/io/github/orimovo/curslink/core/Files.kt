package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import java.io.File
import java.io.FileOutputStream
import java.util.Base64

/** Listed in hello.features: this phone takes dragged files ("drag" and "file_*" frames). */
const val FILES_FEATURE = "files"
/** ...a dragged file's bytes come only once it's let go of here (drag {card, id}, then drag {drop, id}). */
const val DROP_FEATURE = "drop"
/** ...and it acknowledges bytes as they arrive (file_ack), so the host keeps little in flight. */
const val FILE_ACK_FEATURE = "file_ack"

/** What's being dragged, as the host's card says (curslink/files/meta.py card()). */
data class DragCard(val name: String, val kind: String, val size: Long, val count: Int, val dir: Boolean) {
    companion object {
        fun parse(o: JsonObject?): DragCard? {
            o ?: return null
            fun s(k: String) = (o[k] as? JsonPrimitive)?.takeIf { it.isString }?.content
            fun n(k: String) = (o[k] as? JsonPrimitive)?.takeIf { !it.isString && it.booleanOrNull == null }?.content?.toDoubleOrNull()
            val name = s("name") ?: return null
            return DragCard(
                name = name, kind = s("kind") ?: "File", size = (n("size") ?: 0.0).toLong(),
                count = (n("count") ?: 1.0).toInt().coerceAtLeast(1),
                dir = (o["dir"] as? JsonPrimitive)?.booleanOrNull == true,
            )
        }
    }
}

/** What the phone shows of a drag. Called on the link's thread. */
interface DropHandler {
    /** A drag is over this phone: show [card] following the pointer (null: take it down). */
    fun card(card: DragCard?)
    /** Files are arriving (let go of here): [received] of [total] bytes so far. */
    fun progress(id: String, card: DragCard?, received: Long, total: Long) {}
    /** All arrived and saved: their names, and where (in words). */
    fun saved(id: String, names: List<String>, where: String) {}
    fun failed(id: String, why: String) {}
}

/**
 * Where arriving files are written, then published. The default keeps them
 * in [folder] (tests, and phones without MediaStore); the app hands each
 * finished one to MediaStore's Downloads.
 */
open class FileStore(val folder: File, private val staging: File = File(folder, ".curslink-partial")) {
    /** A fresh, private folder for one transfer. */
    open fun begin(id: String): File = File(staging, id).apply { deleteRecursively(); mkdirs() }

    /** Every file of a transfer is in [dir] (top-level [tops]): move them where they belong. Their saved names. */
    open fun publish(dir: File, tops: List<String>): List<String> {
        folder.mkdirs()
        return tops.mapNotNull { top ->
            val src = File(dir, top)
            if (!src.exists()) return@mapNotNull null
            val target = unique(folder, top)
            if (!src.renameTo(target)) { src.copyRecursively(target, overwrite = false); src.deleteRecursively() }
            target.name
        }.also { dir.deleteRecursively() }
    }

    open fun abort(dir: File) { dir.deleteRecursively() }

    /** Where they end up, in words. */
    open val where: String get() = folder.path

    companion object {
        fun unique(folder: File, name: String): File {
            var target = File(folder, name)
            if (!target.exists()) return target
            val dot = name.indexOf('.', 1).takeIf { it > 0 } ?: name.length
            for (n in 1 until 10000) {
                target = File(folder, "${name.substring(0, dot)} ($n)${name.substring(dot)}")
                if (!target.exists()) return target
            }
            throw FileTransferException("too many copies of $name already")
        }
    }
}

class FileTransferException(message: String) : Exception(message)

/**
 * The phone's side of a file drag, like curslink/files/transfer.py's
 * FileReceiver plus the card: while a drag from another device is held over
 * the phone, its card follows the pointer; letting go here brings the files
 * (file_begin, file_item, file_data..., file_end), written to a private
 * folder and only published to Downloads once every byte is in, so a drag
 * that's cancelled or a link that drops leaves nothing half written. A drag
 * that only passes over the phone on its way elsewhere leaves nothing at all.
 */
class FileSession(private val handler: DropHandler, private val store: FileStore) {
    private class Incoming(
        val id: String, val count: Int, val total: Long, val dir: File, val ack: Boolean, val card: DragCard?,
    ) {
        var received = 0L
        var index = -1
        var size = 0L
        var written = 0L
        var out: FileOutputStream? = null
        val tops = ArrayList<String>()
    }

    private val active = HashMap<String, Incoming>()
    private var carried: Pair<String?, DragCard>? = null
    private var lastProgress = 0L

    /** A reply to send back, if the frame needs one. */
    @Synchronized
    fun handle(f: Frame): Pair<String, Map<String, JsonElement>>? {
        val id = f.str("id")
        if (f.t == "drag") {
            drag(f, id)
            return null
        }
        if (id == null || !ID.matches(id)) return null
        return try {
            when (f.t) {
                "file_begin" -> { begin(id, f); null }
                "file_cancel" -> { fail(id, "the other device stopped: ${f.str("reason") ?: ""}".trimEnd(':', ' '), reply = false); null }
                "file_item" -> { item(active[id] ?: return null, f); null }
                "file_data" -> {
                    val inc = active[id] ?: return null
                    data(inc, f)
                    if (inc.ack) "file_ack" to jsonOf("id" to id, "n" to inc.received) else null
                }
                "file_end" -> end(active[id] ?: return null)
                else -> null
            }
        } catch (e: Exception) {
            fail(id, e.message ?: e.javaClass.simpleName)
        }
    }

    private fun drag(f: Frame, id: String?) {
        when {
            f.bool("drop") == true -> {
                // Let go of here: its bytes follow. Until they're in, the card shows how far along they are.
                val c = carried?.takeIf { it.first == id }?.second
                carried = null
                handler.card(null)
                if (id != null) handler.progress(id, c, 0, c?.size ?: 0)
            }
            f.bool("hide") == true -> {
                if (id == null || carried?.first == null || carried?.first == id) {
                    carried = null
                    handler.card(null)
                }
            }
            else -> DragCard.parse(f.obj("card"))?.let {
                carried = id to it
                handler.card(it)
            }
        }
    }

    private fun begin(id: String, f: Frame) {
        if (id in active) throw FileTransferException("duplicate transfer id")
        if (active.size >= MAX_ACTIVE) throw FileTransferException("too many transfers at once")
        val count = f.num("count")?.toInt() ?: throw FileTransferException("bad file_begin")
        val total = f.num("total")?.toLong() ?: throw FileTransferException("bad file_begin")
        if (count < 1 || count > MAX_FILES || total < 0) throw FileTransferException("bad file_begin")
        val free = store.folder.let { it.mkdirs(); it.usableSpace }
        if (free in 1 until total + SPARE_BYTES) throw FileTransferException("not enough space on the phone")
        active[id] = Incoming(id, count, total, store.begin(id), f.bool("ack") == true, DragCard.parse(f.obj("card")))
    }

    private fun item(inc: Incoming, f: Frame) {
        val i = f.num("i")?.toInt()
        val size = f.num("size")?.toLong()
        if (i != inc.index + 1 || size == null || size < 0 || i >= inc.count) throw FileTransferException("files arrived out of order")
        close(inc)
        val parts = safeParts(f.str("path"))
        if (parts[0] !in inc.tops) inc.tops.add(parts[0])
        val target = File(inc.dir, parts.joinToString(File.separator))
        target.parentFile?.mkdirs()
        if (target.exists()) throw FileTransferException("${parts.joinToString("/")} sent twice")
        inc.out = FileOutputStream(target)
        inc.index = i
        inc.size = size
        inc.written = 0
    }

    private fun data(inc: Incoming, f: Frame) {
        val out = inc.out
        if (f.num("i")?.toInt() != inc.index || out == null) throw FileTransferException("data for a file that isn't open")
        val chunk = try {
            Base64.getDecoder().decode(f.str("data") ?: "")
        } catch (e: IllegalArgumentException) {
            throw FileTransferException("bad file_data")
        }
        if (inc.written + chunk.size > inc.size) throw FileTransferException("more data than the file's size")
        out.write(chunk)
        inc.written += chunk.size
        inc.received += chunk.size
        if (inc.received > inc.total) throw FileTransferException("more data than announced")
        val now = System.nanoTime()
        if (now - lastProgress > 100_000_000L) {
            lastProgress = now
            handler.progress(inc.id, inc.card, inc.received, inc.total)
        }
    }

    private fun end(inc: Incoming): Pair<String, Map<String, JsonElement>> {
        close(inc)
        if (inc.index != inc.count - 1 || inc.received != inc.total) throw FileTransferException("the transfer ended early")
        active.remove(inc.id)
        val saved = store.publish(inc.dir, inc.tops)
        handler.saved(inc.id, saved, store.where)
        return "file_result" to jsonOf("id" to inc.id, "ok" to true, "saved" to saved.take(50), "folder" to store.where)
    }

    private fun close(inc: Incoming) {
        val out = inc.out ?: return
        inc.out = null
        out.close()
        if (inc.written != inc.size) throw FileTransferException("a file arrived incomplete")
    }

    private fun fail(id: String, why: String, reply: Boolean = true): Pair<String, Map<String, JsonElement>>? {
        active.remove(id)?.let { inc ->
            runCatching { inc.out?.close() }
            store.abort(inc.dir)
            handler.failed(id, why)
        }
        return if (reply) "file_result" to jsonOf("id" to id, "ok" to false, "error" to why.take(300)) else null
    }

    /** The pointer went to another device: a card over this phone goes with it (the files, if let go of here, still come). */
    @Synchronized
    fun left() {
        if (carried != null) {
            carried = null
            handler.card(null)
        }
    }

    /** The link dropped: nothing more is coming. */
    @Synchronized
    fun reset() {
        for (id in active.keys.toList()) fail(id, "the connection dropped", reply = false)
        if (carried != null) {
            carried = null
            handler.card(null)
        }
    }

    companion object {
        const val MAX_FILES = 10000
        const val MAX_ACTIVE = 4
        const val SPARE_BYTES = 64L * 1024 * 1024
        private val ID = Regex("[0-9A-Za-z_-]{1,40}")
        private val BAD = Regex("[<>:\"/\\\\|?*\\x00-\\x1f]")

        /** A sender's relative path -> safe path parts, like transfer.py safe_parts. */
        fun safeParts(path: String?): List<String> {
            if (path.isNullOrEmpty() || path.startsWith("/") || '\\' in path) throw FileTransferException("bad path $path")
            val parts = path.split("/").map { raw ->
                if (raw == "" || raw == "." || raw == "..") throw FileTransferException("bad path $path")
                var part = BAD.replace(raw, "_").trimEnd(' ', '.').ifEmpty { "_" }
                while (part.toByteArray(Charsets.UTF_8).size > 240) part = part.dropLast(1)
                part
            }
            if (parts.size > 64) throw FileTransferException("path too deep")
            return parts
        }
    }
}
