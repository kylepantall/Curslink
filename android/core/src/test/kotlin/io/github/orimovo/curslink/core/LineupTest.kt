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
        val s = LineupSession { id, cancel -> asked += id to cancel }
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
        val s = LineupSession { id, cancel -> asked += id to cancel }
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
}
