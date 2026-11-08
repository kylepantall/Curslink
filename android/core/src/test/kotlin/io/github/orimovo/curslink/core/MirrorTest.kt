package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import java.util.Base64
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class MirrorTest {
    private class Seen : MirrorHandler {
        var captures = listOf<MirrorCapture>()
        var shows = listOf<MirrorShow>()
        val images = mutableListOf<Pair<String, Int>>()
        var pointer: Triple<String, Double?, Double?>? = null
        override fun state(captures: List<MirrorCapture>, shows: List<MirrorShow>) { this.captures = captures; this.shows = shows }
        override fun image(id: String, jpeg: ByteArray) { images += id to jpeg.size }
        override fun pointer(id: String, x: Double?, y: Double?) { pointer = Triple(id, x, y) }
    }

    private fun frame(t: String, vararg pairs: Pair<String, Any?>) = Frame(t, JsonObject(jsonOf(*pairs)))

    @Test
    fun theHostsStateIsReadAndFramesAreShownAndAnswered() {
        val seen = Seen()
        val m = MirrorSession(seen)
        m.handle(frame("mirror",
            "captures" to listOf(mapOf("id" to "phone:main>pc:main", "os" to listOf(0, 0, 120, 80), "fps" to 60)),
            "shows" to listOf(mapOf("id" to "pc:main>phone:main", "os" to listOf(10, 20, 100, 50), "label" to "PC",
                "edges" to listOf("right")))))
        assertEquals(MirrorCapture("phone:main>pc:main", 0.0, 0.0, 120.0, 80.0, 30), seen.captures.single())
        assertEquals(MirrorShow("pc:main>phone:main", 10.0, 20.0, 100.0, 50.0, "PC", setOf("right")), seen.shows.single())
        val jpg = Base64.getEncoder().encodeToString(byteArrayOf(1, 2, 3))
        val reply = m.handle(frame("mirror_frame", "id" to "pc:main>phone:main", "jpg" to jpg, "w" to 1, "h" to 1))
        assertEquals("mirror_ack", reply?.first)
        assertEquals(listOf("pc:main>phone:main" to 3), seen.images)
        // A frame for a mirror that's gone is still answered, but not shown.
        assertEquals("mirror_ack", m.handle(frame("mirror_frame", "id" to "old", "jpg" to jpg))?.first)
        assertEquals(1, seen.images.size)
        m.handle(frame("mirror_pointer", "id" to "pc:main>phone:main", "x" to 0.5, "y" to 0.25))
        assertEquals(Triple("pc:main>phone:main", 0.5, 0.25), seen.pointer)
        m.handle(frame("mirror_pointer", "id" to "pc:main>phone:main", "hide" to true))
        assertNull(seen.pointer!!.second)
    }

    @Test
    fun capturesKeepTwoFramesInFlightAndSendTheNewestWaitingOne() {
        val m = MirrorSession(Seen())
        val sent = mutableListOf<Map<String, JsonElement>>()
        m.send = { t, data -> assertEquals("mirror_frame", t); sent += data }
        m.handle(frame("mirror", "captures" to listOf(mapOf("id" to "c", "os" to listOf(0, 0, 10, 10))), "shows" to emptyList<Any>()))
        repeat(4) { m.offer("c", byteArrayOf(it.toByte()), 10, 10) }
        assertEquals(MIRROR_IN_FLIGHT, sent.size)
        assertTrue(!m.ready("c"))
        m.handle(frame("mirror_ack", "id" to "c"))
        assertEquals(3, sent.size)
        assertEquals(Base64.getEncoder().encodeToString(byteArrayOf(3)), sent.last()["jpg"]?.strOrNull())
        m.offer("gone", byteArrayOf(9), 1, 1)
        assertEquals(3, sent.size)
    }

    @Test
    fun aDroppedLinkTakesEverythingDown() {
        val seen = Seen()
        val m = MirrorSession(seen)
        m.handle(frame("mirror", "captures" to emptyList<Any>(),
            "shows" to listOf(mapOf("id" to "s", "os" to listOf(0, 0, 10, 10)))))
        m.reset()
        assertTrue(seen.shows.isEmpty() && seen.captures.isEmpty())
    }
}
