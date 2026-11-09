package io.github.orimovo.curslink.core

import java.nio.file.Files
import java.util.Base64
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

class FilesTest {
    private class Ui : DropHandler {
        val cards = ArrayList<DragCard?>()
        val saved = ArrayList<List<String>>()
        val failed = ArrayList<String>()
        override fun card(card: DragCard?) { cards.add(card) }
        override fun saved(id: String, names: List<String>, where: String) { saved.add(names) }
        override fun failed(id: String, why: String) { failed.add(why) }
    }

    private fun frame(t: String, vararg pairs: Pair<String, Any?>) =
        Frames.decode(Frames.encode(t, jsonOf(*pairs)).toString(Charsets.UTF_8).trimEnd('\n'))

    private val card = mapOf("name" to "photo.jpg", "kind" to "JPG image", "size" to 5, "count" to 1, "dir" to false,
        "items" to listOf(mapOf("name" to "photo.jpg", "dir" to false)))

    private fun send(files: FileSession, id: String, path: String, bytes: ByteArray, ack: Boolean = true): List<Pair<String, Map<String, kotlinx.serialization.json.JsonElement>>?> =
        listOf(
            files.handle(frame("file_begin", "id" to id, "count" to 1, "total" to bytes.size, "card" to card, "from" to "PC", "ack" to ack)),
            files.handle(frame("file_item", "id" to id, "i" to 0, "path" to path, "size" to bytes.size)),
            files.handle(frame("file_data", "id" to id, "i" to 0, "data" to Base64.getEncoder().encodeToString(bytes))),
            files.handle(frame("file_end", "id" to id)),
        )

    @Test
    fun aDragPassingOverShowsItsCardAndLeavesNothing() {
        val dir = Files.createTempDirectory("drop").toFile()
        val ui = Ui()
        val files = FileSession(ui, FileStore(dir))
        files.handle(frame("drag", "card" to card, "id" to "t1"))
        assertEquals("photo.jpg", ui.cards.last()?.name)
        assertEquals("JPG image", ui.cards.last()?.kind)
        files.handle(frame("drag", "hide" to true, "id" to "t1"))
        assertNull(ui.cards.last())
        assertTrue(dir.listFiles()!!.none { !it.name.startsWith(".") })
    }

    @Test
    fun letGoHereTheFilesArriveAndAreSavedWhole() {
        val dir = Files.createTempDirectory("drop").toFile()
        val ui = Ui()
        val files = FileSession(ui, FileStore(dir))
        files.handle(frame("drag", "card" to card, "id" to "t2"))
        files.handle(frame("drag", "drop" to true, "id" to "t2"))
        assertNull(ui.cards.last())                                  // the card gives way to progress
        val replies = send(files, "t2", "photo.jpg", "hello".toByteArray())
        assertEquals("file_ack", replies[2]?.first)
        assertEquals("5", replies[2]?.second?.get("n").toString())
        assertEquals("file_result", replies[3]?.first)
        assertEquals("true", replies[3]?.second?.get("ok").toString())
        assertEquals("hello", java.io.File(dir, "photo.jpg").readText())
        // A second drop of the same name doesn't overwrite it.
        send(files, "t3", "photo.jpg", "again".toByteArray())
        assertEquals("again", java.io.File(dir, "photo (1).jpg").readText())
        assertEquals(listOf(listOf("photo.jpg"), listOf("photo (1).jpg")), ui.saved)
    }

    @Test
    fun foldersKeepTheirShape() {
        val dir = Files.createTempDirectory("drop").toFile()
        val files = FileSession(Ui(), FileStore(dir))
        files.handle(frame("file_begin", "id" to "f1", "count" to 2, "total" to 4, "from" to "Mac"))
        files.handle(frame("file_item", "id" to "f1", "i" to 0, "path" to "Trip/a.txt", "size" to 2))
        files.handle(frame("file_data", "id" to "f1", "i" to 0, "data" to "aGk="))
        files.handle(frame("file_item", "id" to "f1", "i" to 1, "path" to "Trip/days/b.txt", "size" to 2))
        files.handle(frame("file_data", "id" to "f1", "i" to 1, "data" to "eW8="))
        val done = files.handle(frame("file_end", "id" to "f1"))
        assertEquals("true", done?.second?.get("ok").toString())
        assertEquals("yo", java.io.File(dir, "Trip/days/b.txt").readText())
    }

    @Test
    fun aBadOrCancelledTransferLeavesNothingBehind() {
        val dir = Files.createTempDirectory("drop").toFile()
        val ui = Ui()
        val files = FileSession(ui, FileStore(dir))
        files.handle(frame("file_begin", "id" to "b1", "count" to 1, "total" to 2, "from" to "Mac"))
        val refused = files.handle(frame("file_item", "id" to "b1", "i" to 0, "path" to "../evil", "size" to 2))
        assertEquals("false", refused?.second?.get("ok").toString())
        files.handle(frame("file_begin", "id" to "b2", "count" to 1, "total" to 2, "from" to "Mac"))
        files.handle(frame("file_item", "id" to "b2", "i" to 0, "path" to "a.txt", "size" to 2))
        assertNull(files.handle(frame("file_cancel", "id" to "b2", "reason" to "gone")))
        files.handle(frame("file_begin", "id" to "b3", "count" to 1, "total" to 2, "from" to "Mac"))
        files.handle(frame("file_item", "id" to "b3", "i" to 0, "path" to "x.bin", "size" to 2))
        val lying = files.handle(frame("file_data", "id" to "b3", "i" to 0, "data" to "YWJj"))   // 3 bytes, not 2
        assertNotNull(lying)
        assertEquals("false", lying.second["ok"].toString())
        assertTrue(dir.walk().none { it.isFile })
        assertEquals(3, ui.failed.size)
    }

    @Test
    fun theLinkDroppingMidwayCleansUp() {
        val dir = Files.createTempDirectory("drop").toFile()
        val ui = Ui()
        val files = FileSession(ui, FileStore(dir))
        files.handle(frame("drag", "card" to card, "id" to "d1"))
        files.handle(frame("file_begin", "id" to "d1", "count" to 1, "total" to 9, "from" to "Mac"))
        files.reset()
        assertNull(ui.cards.last())
        assertEquals(listOf("the connection dropped"), ui.failed)
        assertTrue(dir.walk().none { it.isFile })
    }

    @Test
    fun theSessionRoutesDragFramesAndHidesTheCardOnLeave() {
        val dir = Files.createTempDirectory("drop").toFile()
        val ui = Ui()
        val handler = object : InputHandler {
            override fun enter(x: Double, y: Double, effect: String) {}
            override fun leave(effect: String, x: Double?, y: Double?) {}
            override fun move(x: Double, y: Double) {}
            override fun button(name: String, pressed: Boolean) {}
            override fun scroll(dx: Double, dy: Double) {}
            override fun key(key: String, pressed: Boolean) {}
        }
        val session = AgentSession(handler, files = FileSession(ui, FileStore(dir)))
        session.handle(frame("drag", "card" to card, "id" to "s1"))
        assertEquals("photo.jpg", ui.cards.last()?.name)
        session.handle(frame("leave", "effect" to "none"))
        assertNull(ui.cards.last())
        val ack = session.handle(frame("file_begin", "id" to "s2", "count" to 1, "total" to 1, "from" to "Mac", "ack" to true))
        assertNull(ack)
    }

    @Test
    fun pathsAreMadeSafeLikeTheHostDoes() {
        assertEquals(listOf("a_b.txt"), FileSession.safeParts("a:b.txt"))
        assertEquals(listOf("dir", "x"), FileSession.safeParts("dir/x"))
        for (bad in listOf("", "/abs", "a/../b", "a\\b")) {
            assertTrue(runCatching { FileSession.safeParts(bad) }.isFailure, bad)
        }
    }
}
