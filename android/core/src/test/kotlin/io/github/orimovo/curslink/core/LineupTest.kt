package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import java.io.ByteArrayOutputStream
import java.util.Base64
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class LineupTest {
    private fun frame(t: String, vararg pairs: Pair<String, Any?>) = Frame(t, JsonObject(jsonOf(*pairs)))

    @Test
    fun aPhotoGoesOutInFramesThatFit() {
        val asked = mutableListOf<Pair<String, Boolean>>()
        val s = LineupSession { id, cancel, _ -> asked += id to cancel }
        val frames = mutableListOf<Pair<String, Map<String, JsonElement>>>()
        s.send = { t, data -> frames += t to data; Frames.encode(t, data) }
        s.handle(frame("lineup_camera", "id" to "L1"))
        assertEquals(listOf("L1" to false), asked)
        assertEquals("L1", s.waiting)
        val jpeg = ByteArray(100_000) { (it * 7).toByte() }
        s.photo("L1", jpeg)
        assertNull(s.waiting)
        assertEquals("lineup_photo", frames.first().first)
        val got = ByteArrayOutputStream()
        frames.drop(1).forEach { (t, data) ->
            assertEquals("lineup_data", t)
            got.write(Base64.getDecoder().decode(Frame(t, JsonObject(data)).str("data")))
        }
        assertEquals(true, Frame("x", JsonObject(frames.last().second)).bool("end"))
        assertTrue(jpeg.contentEquals(got.toByteArray()))
    }

    @Test
    fun cancelsAndRequests() {
        val asked = mutableListOf<Pair<String, Boolean>>()
        val s = LineupSession { id, cancel, _ -> asked += id to cancel }
        val frames = mutableListOf<String>()
        s.send = { t, _ -> frames += t }
        assertFalse(s.request())  // an older host
        s.hostLines = true
        assertTrue(s.request())
        s.handle(frame("lineup_camera", "id" to "L2", "cancel" to true))  // not waiting on it
        s.handle(frame("lineup_camera", "id" to "L2"))
        s.handle(frame("lineup_camera", "id" to "L2", "cancel" to true))
        assertEquals(listOf("L2" to false, "L2" to true), asked)
        s.cancel("L3")
        assertEquals(listOf("lineup_request", "lineup_photo"), frames)
    }

    @Test
    fun liveFramesGoOutNumberedAndTheHostSaysHowItsGoing() {
        val asked = mutableListOf<Triple<String, Boolean, Boolean>>()
        val s = LineupSession { id, cancel, live -> asked += Triple(id, cancel, live) }
        val frames = mutableListOf<Pair<String, Map<String, JsonElement>>>()
        s.send = { t, data -> frames += t to data; Frames.encode(t, data) }
        s.handle(frame("lineup_camera", "id" to "L1", "live" to true))
        assertEquals(listOf(Triple("L1", false, true)), asked)
        val jpeg = ByteArray(70_000) { (it * 3).toByte() }
        s.frame("L1", 4, jpeg, 1180.5)
        val first = Frame(frames.first().first, JsonObject(frames.first().second))
        assertEquals("lineup_photo", first.t)
        assertEquals(4.0, first.num("seq"))
        assertEquals(1180.5, first.num("focal"))
        assertEquals(70_000.0, first.num("size"))
        assertEquals(3, frames.size - 1)  // 32 KiB at a time
        assertEquals("L1", s.waiting)  // still going
        s.frame("L1", 5, ByteArray(10), null)  // a camera that doesn't say
        assertNull(frames[frames.size - 2].second["focal"])

        val seen = mutableListOf<LineupStatus>()
        s.onStatus = { seen += it }
        s.handle(Frames.decode("""{"t":"lineup_live","id":"L1","seq":5,"progress":0.4,"hint":"Move the phone slowly in a figure of 8","screens":[[1,"locked"],[2,"placing"],["x",3]]}"""))
        assertEquals(LineupStatus("L1", 5, 0.4, "Move the phone slowly in a figure of 8",
            listOf(1 to "locked", 2 to "placing"), false), seen.last())
        s.handle(Frames.decode("""{"t":"lineup_live","id":"L1","progress":1,"hint":"Lined up","screens":[],"done":true}"""))
        assertTrue(seen.last().done)
        assertNull(seen.last().seq)
        assertNull(s.waiting)
        assertTrue(!seen.last().buzz)
        // The last step ("where you sit"): the phone is at a face, so it's told to shake when it's found.
        s.handle(Frames.decode("""{"t":"lineup_live","id":"L1","progress":1,"hint":"Got you: 95 cm from screen 1","screens":[],"buzz":true,"done":true}"""))
        assertTrue(seen.last().buzz && seen.last().done)
        frames.clear()
        s.done("L1")
        assertEquals("lineup_photo", frames.single().first)
        assertEquals(true, Frame("x", JsonObject(frames.single().second)).bool("done"))
    }
}
