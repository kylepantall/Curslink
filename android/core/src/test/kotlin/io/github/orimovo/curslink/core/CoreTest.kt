package io.github.orimovo.curslink.core

import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class CoreTest {
    private val host = DeviceInfo("desk", "Desk Mac", "host")
    private val phone = DeviceInfo("phone", "Pixel", "agent")

    @Test
    fun pairingByCodeGivesBothSidesTheSameKey() {
        var shown = ""
        var theirs: PairResult? = null
        val server = PairingServer({ phone }, { jsonOf("platform" to "Android 15") }, {
            Thread.sleep(100)
            shown
        }, { r, _ -> theirs = r }, port = 0)
        server.start()
        try {
            Socket("127.0.0.1", server.boundPort).use { s ->
                val io = FrameIO(s.getInputStream(), s.getOutputStream())
                val mine = Pairing.initiate(io, host, showCode = { code, _ -> shown = code })
                Thread.sleep(100)
                assertEquals(phone, mine.peer)
                assertEquals("Android 15", mine.extra["platform"]?.strOrNull())
                assertContentEquals(mine.key, theirs!!.key)
                assertEquals(7, shown.length)
            }
        } finally {
            server.close()
        }
    }

    @Test
    fun aWrongCodeIsRefused() {
        val server = PairingServer({ phone }, { emptyMap() }, { "000000x" }, { _, _ -> error("paired") }, port = 0)
        server.start()
        try {
            Socket("127.0.0.1", server.boundPort).use { s ->
                val io = FrameIO(s.getInputStream(), s.getOutputStream())
                // One in a million the code really is 000000.
                assertFailsWith<PairingException> { Pairing.initiate(io, host) }
            }
        } finally {
            server.close()
        }
    }

    @Test
    fun udpDatagramsRoundTripAndRejectTampering() {
        val offer = UdpOffer(1234, ByteArray(8) { it.toByte() }, ByteArray(32) { 7 })
        val udp = AgentUdp(offer) { _, _, _ -> }
        val body = java.nio.ByteBuffer.allocate(24).putDouble(1.5).putDouble(2.5).putDouble(3.0).array()
        val packet = udp.seal(AgentUdp.MOVE, 5, body)
        val (kind, seq, opened) = udp.open(packet)!!
        assertEquals(AgentUdp.MOVE, kind)
        assertEquals(5L, seq)
        assertContentEquals(body, opened)
        packet[25] = (packet[25] + 1).toByte()
        assertNull(udp.open(packet))
    }

    @Test
    fun udpProbesFastOnlyWhileInputIsHere() {
        val offer = UdpOffer(1234, ByteArray(8) { it.toByte() }, ByteArray(32) { 7 })
        var here = false
        val udp = AgentUdp(offer, active = { here }) { _, _, _ -> }
        assertFalse(udp.warming())
        here = true
        assertTrue(udp.warming())
    }

    @Test
    fun invitesParse() {
        val inv = Invite.parse("curslink://pair?v=1&id=desk&n=Kyle%27s%20Mac&a=192.168.1.5,10.0.0.2&p=50123&c=24850&t=AAECAwQFBgcICQoLDA0ODw&r=relay.example.com:24852&k=abc")
        assertEquals("Kyle's Mac", inv.hostName)
        assertEquals(listOf("192.168.1.5", "10.0.0.2"), inv.addresses)
        assertEquals(50123, inv.pairingPort)
        assertEquals(16, inv.token.size)
        assertEquals("relay.example.com:24852", inv.relay)
        assertFailsWith<IllegalArgumentException> { Invite.parse("https://example.com") }
    }

    @Test
    fun relayAddresses() {
        assertEquals("relay.example.com" to 24852, Relay.parseAddress("relay.example.com"))
        assertEquals("1.2.3.4" to 99, Relay.parseAddress("1.2.3.4:99"))
        assertEquals("::1" to 5, Relay.parseAddress("[::1]:5"))
    }

    @Test
    fun sessionLetsGoOfHeldKeysOnLeave() {
        val events = mutableListOf<String>()
        val s = AgentSession(object : InputHandler {
            override fun enter(x: Double, y: Double, effect: String) { events += "enter" }
            override fun leave(effect: String, x: Double?, y: Double?) { events += "leave" }
            override fun move(x: Double, y: Double) { events += "move" }
            override fun button(name: String, pressed: Boolean) { events += "button $name $pressed" }
            override fun scroll(dx: Double, dy: Double) {}
            override fun key(key: String, pressed: Boolean) { events += "key $key $pressed" }
        })
        fun f(line: String) = s.handle(Frames.decode(line))
        f("""{"t":"enter","x":1,"y":2,"effect":"pop"}""")
        f("""{"t":"key","key":"shift","pressed":true}""")
        f("""{"t":"key","key":"b","pressed":false}""")
        f("""{"t":"move","x":1,"y":2,"ts":10}""")
        f("""{"t":"move","x":1,"y":2,"ts":5}""")
        f("""{"t":"leave","effect":"none"}""")
        assertEquals(listOf("enter", "key shift true", "move", "key shift false", "leave"), events)
        val pong = f("""{"t":"ping","n":3,"ts":1.5}""")!!
        assertEquals("pong", pong.first)
        assertTrue(pong.second.keys == setOf("n", "ts"))
    }

    @Test
    fun edgeFramesReachTheHandler() {
        val got = mutableListOf<EdgeCommand>()
        val s = AgentSession(object : InputHandler {
            override fun enter(x: Double, y: Double, effect: String) {}
            override fun leave(effect: String, x: Double?, y: Double?) {}
            override fun move(x: Double, y: Double) {}
            override fun button(name: String, pressed: Boolean) {}
            override fun scroll(dx: Double, dy: Double) {}
            override fun key(key: String, pressed: Boolean) {}
            override fun edge(cmd: EdgeCommand) { got += cmd }
        })
        fun f(line: String) = s.handle(Frames.decode(line))
        f("""{"t":"edge","role":"from","rect":[0,0,1080,2400],"side":"left","a":0,"b":2400,"at":900,"near":0.5,"name":"Mac","kind":"mac"}""")
        f("""{"t":"edge","role":"sideways","rect":[0,0,1,1],"side":"left","a":0,"b":1,"at":0,"near":1}""")
        f("""{"t":"edge","flash":true}""")
        f("""{"t":"edge","hide":true}""")
        assertEquals(3, got.size)
        assertEquals("from", got[0].role)
        assertEquals(listOf(0.0, 0.0, 1080.0, 2400.0), got[0].rect)
        assertEquals(900.0, got[0].at, 0.0)
        assertEquals("Mac", got[0].name)
        assertTrue(got[1].flash)
        assertTrue(got[2].hide)
    }

    @Test
    fun udpOfferAddressMustBeAProbeableIpv4Literal() {
        assertTrue(AgentUdp.probeable("192.168.68.72"))
        for (bad in listOf("host.local", "127.0.0.1", "0.0.0.0", "224.0.0.1", "1.2.3", "1.2.3.256", "::1")) {
            assertFalse(AgentUdp.probeable(bad), bad)
        }
    }

    @Test
    fun aDroppedLinkPutsThePointerAwayAndLetsGo() {
        val events = mutableListOf<String>()
        val session = AgentSession(object : InputHandler {
            override fun enter(x: Double, y: Double, effect: String) { events += "enter" }
            override fun leave(effect: String, x: Double?, y: Double?) { events += "leave $effect" }
            override fun move(x: Double, y: Double) {}
            override fun button(name: String, pressed: Boolean) { events += "button $name $pressed" }
            override fun scroll(dx: Double, dy: Double) {}
            override fun key(key: String, pressed: Boolean) { events += "key $key $pressed" }
            override fun edge(cmd: EdgeCommand) { events += if (cmd.hide) "edge hide" else "edge" }
        })
        session.handle(Frames.decode("""{"t":"enter","x":1,"y":2}"""))
        session.handle(Frames.decode("""{"t":"key","key":"a","pressed":true}"""))
        session.dropped()
        assertEquals(listOf("enter", "key a true", "key a false", "leave none", "edge hide"), events)
        events.clear()
        session.dropped()  // not here any more: only the edge light is put away
        assertEquals(listOf("edge hide"), events)
    }
}
