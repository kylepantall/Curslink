package io.github.orimovo.curslink.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream

/**
 * The wire format shared with the Python host (curslink/protocol.py):
 * newline-delimited UTF-8 JSON objects, each with a "t" (type) field.
 */
const val PROTOCOL_VERSION = 1
const val DEFAULT_PORT = 24850
const val DEFAULT_PAIRING_PORT = 24851
const val DEFAULT_RELAY_PORT = 24852
const val MAX_FRAME_BYTES = 64 * 1024

class ProtocolException(message: String, cause: Throwable? = null) : IOException(message, cause)

class Frame(val t: String, val data: JsonObject) {
    fun str(key: String): String? = (data[key] as? JsonPrimitive)?.takeIf { it.isString }?.content
    fun num(key: String): Double? = (data[key] as? JsonPrimitive)?.takeIf { !it.isString && it.booleanOrNull == null }?.doubleOrNull
    fun bool(key: String): Boolean? = (data[key] as? JsonPrimitive)?.takeIf { !it.isString }?.booleanOrNull
    fun obj(key: String): JsonObject? = data[key] as? JsonObject
    operator fun get(key: String): JsonElement? = data[key]
    override fun toString() = "Frame($t, $data)"
}

object Frames {
    private val json = Json { ignoreUnknownKeys = true }

    fun encode(t: String, data: Map<String, JsonElement> = emptyMap()): ByteArray {
        val obj = JsonObject(linkedMapOf<String, JsonElement>("t" to JsonPrimitive(t)) + data)
        val raw = (obj.toString() + "\n").toByteArray(Charsets.UTF_8)
        if (raw.size >= MAX_FRAME_BYTES) throw ProtocolException("frame too large")
        return raw
    }

    fun decode(line: String): Frame {
        val obj = try {
            json.parseToJsonElement(line).jsonObject
        } catch (e: Exception) {
            throw ProtocolException("invalid JSON frame", e)
        }
        val t = (obj["t"] as? JsonPrimitive)?.takeIf { it.isString }?.content
            ?: throw ProtocolException("frame has no type")
        return Frame(t, JsonObject(obj - "t"))
    }
}

/** Reads one newline-terminated line at a time, never past the newline, so
 *  the stream can switch to TLS straight after (the relay handshake). */
class LineReader(private val input: InputStream) {
    private val buf = java.io.ByteArrayOutputStream()

    /** The next line without its newline, or null at a clean end of stream. */
    fun readLine(): String? {
        buf.reset()
        while (true) {
            val b = input.read()
            if (b < 0) {
                if (buf.size() == 0) return null
                throw ProtocolException("connection closed mid-frame")
            }
            if (b == '\n'.code) return buf.toString(Charsets.UTF_8)
            buf.write(b)
            if (buf.size() > MAX_FRAME_BYTES) throw ProtocolException("frame too large")
        }
    }

    fun readFrame(): Frame? = readLine()?.let { Frames.decode(it) }
}

/** Frames over a pair of streams; writes are serialised. */
class FrameIO(input: InputStream, private val output: OutputStream) {
    private val reader = LineReader(java.io.BufferedInputStream(input, 16 * 1024))

    fun read(): Frame? = reader.readFrame()

    fun send(t: String, data: Map<String, JsonElement> = emptyMap()) {
        val raw = Frames.encode(t, data)
        synchronized(output) {
            output.write(raw)
            output.flush()
        }
    }
}

fun jsonOf(vararg pairs: Pair<String, Any?>): Map<String, JsonElement> =
    pairs.filter { it.second != null }.associate { (k, v) -> k to toJson(v) }

fun toJson(v: Any?): JsonElement = when (v) {
    null -> kotlinx.serialization.json.JsonNull
    is JsonElement -> v
    is String -> JsonPrimitive(v)
    is Number -> JsonPrimitive(v)
    is Boolean -> JsonPrimitive(v)
    is Map<*, *> -> JsonObject(v.entries.associate { (k, x) -> k.toString() to toJson(x) })
    is Iterable<*> -> kotlinx.serialization.json.JsonArray(v.map { toJson(it) })
    else -> JsonPrimitive(v.toString())
}

internal fun JsonElement.strOrNull(): String? = (this as? JsonPrimitive)?.takeIf { it.isString }?.content
internal fun JsonElement?.numOrNull(): Double? = (this as? JsonPrimitive)?.takeIf { !it.isString }?.let { it.jsonPrimitive.doubleOrNull }
