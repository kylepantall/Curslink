package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class ClipboardTest {
    @Test
    fun copiedTextGoesOutInChunksAndComesBackWhole() {
        val text = "héllo ".repeat(12_000)  // over two chunks
        val got = mutableListOf<String>()
        val receiver = ClipboardSession({ got += it })
        val sender = ClipboardSession({})
        val frames = mutableListOf<Pair<String, Map<String, JsonElement>>>()
        sender.send = { t, data -> frames += t to data; Frames.encode(t, data) }  // each fits a frame
        sender.copied(text)
        assertEquals("clip", frames.first().first)
        assertTrue(frames.size >= 4)
        frames.forEach { (t, data) -> receiver.handle(Frame(t, JsonObject(data))) }
        assertEquals(listOf(text), got)
    }

    @Test
    fun picturesAndReplacedClipsAreIgnored() {
        val got = mutableListOf<String>()
        val s = ClipboardSession({ got += it })
        fun frame(t: String, vararg pairs: Pair<String, Any?>) = Frame(t, JsonObject(jsonOf(*pairs)))
        s.handle(frame("clip", "id" to "img", "kind" to "image", "size" to 3))
        s.handle(frame("clip_data", "id" to "img", "data" to "AAAA", "end" to true))
        s.handle(frame("clip", "id" to "old", "kind" to "text", "size" to 6))
        s.handle(frame("clip_data", "id" to "old", "data" to "YWJj"))
        s.handle(frame("clip", "id" to "new", "kind" to "text", "size" to 2))
        s.handle(frame("clip_data", "id" to "old", "data" to "ZGVm", "end" to true))
        s.handle(frame("clip_data", "id" to "new", "data" to "aGk=", "end" to true))
        assertEquals(listOf("hi"), got)
    }
}
