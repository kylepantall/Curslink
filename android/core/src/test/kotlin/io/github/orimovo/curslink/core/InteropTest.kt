package io.github.orimovo.curslink.core

import java.io.File
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue
import kotlin.test.fail

/**
 * Pairs with and connects to the real Python host (interop/host.py): QR
 * pairing, TLS-PSK with the hello proof, UDP moves, then the relay.
 */
class InteropTest {
    private fun python(): String? {
        val repo = System.getenv("CURSLINK_REPO") ?: return null
        for (exe in listOf("python3", "python")) {
            val ok = runCatching {
                ProcessBuilder(exe, "-c", "import curslink.pairing.qr").directory(File(repo)).start().waitFor(20, TimeUnit.SECONDS)
            }.getOrNull()
            if (ok == true && runCatching {
                    ProcessBuilder(exe, "-c", "import curslink.pairing.qr").directory(File(repo)).start().waitFor() == 0
                }.getOrDefault(false)) return exe
        }
        return null
    }

    @Test
    fun pairsAndTakesInputFromThePythonHost() {
        val exe = python()
        if (exe == null) {
            println("SKIPPED: python3 with curslink installed is needed for the interop test")
            return
        }
        val repo = File(System.getenv("CURSLINK_REPO"))
        val proc = ProcessBuilder(exe, "android/interop/host.py").directory(repo).redirectError(ProcessBuilder.Redirect.INHERIT).start()
        val lines = LinkedBlockingQueue<String>()
        Thread { proc.inputStream.bufferedReader().forEachLine { println("host: $it"); lines.put(it) } }.apply { isDaemon = true; start() }
        fun next(prefix: String): String {
            while (true) {
                val line = lines.poll(20, TimeUnit.SECONDS) ?: fail("the host never said $prefix")
                if (line.startsWith("$prefix ")) return line.removePrefix("$prefix ")
            }
        }
        try {
            val invite = Invite.parse(next("URL"))
            val me = DeviceInfo("pixel-test", "Test Pixel", "agent")
            val screens = listOf(mapOf("x" to 0, "y" to 0, "width" to 411, "height" to 914, "name" to "phone"))
            val (result, address) = PairingNet.pairByQr(invite, me, jsonOf("screens" to screens, "platform" to "Android 15"))
            assertEquals("interop-host", result.peer.deviceId)
            assertTrue(next("PAIRED").let { it.startsWith("pixel-test ") && it.contains("\"width\": 411") })

            val events = LinkedBlockingQueue<String>()
            val handler = object : InputHandler {
                override fun enter(x: Double, y: Double, effect: String) { events.put("enter $x $y $effect") }
                override fun leave(effect: String, x: Double?, y: Double?) { events.put("leave $effect") }
                override fun move(x: Double, y: Double) { events.put("move $x $y") }
                override fun button(name: String, pressed: Boolean) { events.put("button $name $pressed") }
                override fun scroll(dx: Double, dy: Double) { events.put("scroll $dx $dy") }
                override fun key(key: String, pressed: Boolean) { events.put("key $key $pressed") }
            }
            val expected = listOf(
                "enter 10.0 20.0 pop", "move 30.0 40.0", "button left true", "button left false",
                "scroll 0.0 -1.5", "key a true", "key a false", "key shift true", "key shift false", "leave blink",
            )
            fun run(pairing: HostPairing, wantUdp: Boolean) {
                events.clear()
                val link = AgentLink(handler, { pairing }, { Hello("Test Pixel", "Android 15", screens) }, log = { println("link: $it") })
                link.start()
                try {
                    val connected = next("CONNECTED")
                    assertTrue(connected.contains("\"width\": 411"), connected)
                    val sent = next("SENT")
                    assertEquals(wantUdp, sent.contains("true"), "udp in $sent")
                    // A move sent by UDP is repeated over TCP before the click.
                    val got = mutableListOf<String>()
                    while (got.lastOrNull() != "leave blink") {
                        val e = events.poll(10, TimeUnit.SECONDS) ?: break
                        if (e != got.lastOrNull()) got += e
                    }
                    assertEquals(expected, got)
                } finally {
                    link.stop()
                }
            }
            val pairing = HostPairing(invite.hostId, invite.hostName, me.deviceId, result.key, listOf(address), invite.controlPort, invite.relay, invite.relayKey)
            run(pairing, wantUdp = true)
            run(pairing.copy(addresses = emptyList()), wantUdp = false)  // through the relay only
        } finally {
            proc.outputStream.close()
            proc.destroy()
        }
    }
}
