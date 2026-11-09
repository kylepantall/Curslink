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

    @Test
    fun aMirrorBeingLinedUpGetsItsGuidesAndSendsTunes() {
        val seen = Seen()
        val m = MirrorSession(seen)
        val sent = mutableListOf<Pair<String, Map<String, JsonElement>>>()
        m.send = { t, data -> sent += t to data }
        m.handle(frame("mirror", "captures" to emptyList<Any>(), "shows" to listOf(mapOf(
            "id" to "mac:s2>phone:p", "os" to listOf(0, 0, 400, 200), "label" to "Mac", "edges" to listOf("top"),
            "tune" to true, "guides" to listOf(10, 20, 300, 100), "src" to listOf(0, 0, 320, 120)))))
        val show = seen.shows.single()
        assertTrue(show.tune)
        assertEquals(listOf(10.0, 20.0, 300.0, 100.0), show.guides)
        assertEquals(listOf(0.0, 0.0, 320.0, 120.0), show.src)
        val latch = java.util.concurrent.CountDownLatch(1)
        m.send = { t, data -> sent += t to data; latch.countDown() }
        m.tune(show.id, mapOf("mx" to 0.1, "my" to -0.05))
        assertTrue(latch.await(5, java.util.concurrent.TimeUnit.SECONDS))
        assertEquals("mirror_tune", sent.single().first)
        assertEquals("mac:s2>phone:p", sent.single().second["id"]?.strOrNull())
        assertEquals(0.1, sent.single().second["mx"].numOrNull())
        // Not being lined up: no guides.
        m.handle(frame("mirror", "captures" to emptyList<Any>(), "shows" to listOf(mapOf(
            "id" to "mac:s2>phone:p", "os" to listOf(0, 0, 400, 200), "guides" to listOf(1, 1, 1, 1)))))
        assertEquals(false, seen.shows.single().tune)
        assertNull(seen.shows.single().guides)
    }

    @Test
    fun theTunerMovesStretchesAndZoomsLikeTheComputersMirror() {
        val r = listOf(100.0, 100.0, 400.0, 200.0)
        val t = MirrorTuner(grab = 20.0)
        assertTrue(!t.press(r, 50.0, 50.0))  // off the mirror: not taken
        assertTrue(t.press(r, 300.0, 200.0))
        assertEquals(mapOf("mx" to 0.1, "my" to -0.1), t.move(r, 340.0, 180.0))
        assertNull(t.move(r, 340.4, 180.2))  // too small a step
        t.release()
        assertNull(t.move(r, 400.0, 200.0))
        // The right side: bigger about the left one.
        assertTrue(t.press(r, 495.0, 200.0))
        assertEquals(mapOf("grow" to 1.1, "ax" to 0.0, "ay" to 0.5), t.move(r, 535.0, 200.0))
        t.release()
        // Top-left corner: both, about the bottom right.
        assertTrue(t.press(r, 105.0, 105.0))
        assertEquals(mapOf("grow" to 0.9, "ax" to 1.0, "ay" to 1.0), t.move(r, 145.0, 125.0))
        t.release()
        assertEquals(mapOf("grow" to 1.05, "ax" to 0.25, "ay" to 0.5), t.wheel(r, 200.0, 200.0, 1.0))
        assertNull(t.wheel(r, 0.0, 0.0, 1.0))
    }

    @Test
    fun fingersOnThePhoneMoveAndPinchTheMirror() {
        val r = listOf(0.0, 0.0, 400.0, 200.0)
        val t = MirrorTuner(grab = 20.0)
        assertEquals(emptyList(), t.touch(r, doubleArrayOf(100.0), doubleArrayOf(100.0)))  // where it starts
        assertEquals(listOf(mapOf<String, Any?>("mx" to 0.1, "my" to 0.1)), t.touch(r, doubleArrayOf(140.0), doubleArrayOf(120.0)))
        // A second finger: starts again, then spreading them zooms about their middle.
        assertEquals(emptyList(), t.touch(r, doubleArrayOf(100.0, 300.0), doubleArrayOf(100.0, 100.0)))
        assertEquals(listOf(mapOf<String, Any?>("grow" to 2.0, "ax" to 0.5, "ay" to 0.5)),
            t.touch(r, doubleArrayOf(0.0, 400.0), doubleArrayOf(100.0, 100.0)))
        t.touchEnd()
        assertEquals(emptyList(), t.touch(r, doubleArrayOf(10.0), doubleArrayOf(10.0)))
    }
}
