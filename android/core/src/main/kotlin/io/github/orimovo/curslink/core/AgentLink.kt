package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException

/** What pairing with a host leaves us: enough to find it and prove who we are. */
data class HostPairing(
    val hostId: String,
    val hostName: String,
    val myId: String,
    val key: ByteArray,
    val addresses: List<String> = emptyList(),
    val controlPort: Int = DEFAULT_PORT,
    val relay: String = "",
    val relayKey: String = "",
) {
    val hasRelay get() = relay.isNotEmpty() && relayKey.isNotEmpty()
}

/** Where the host's input goes on this device. Called on the link's threads. */
interface InputHandler {
    fun enter(x: Double, y: Double, effect: String)
    fun leave(effect: String, x: Double?, y: Double?)
    fun move(x: Double, y: Double)
    fun button(name: String, pressed: Boolean)
    fun scroll(dx: Double, dy: Double)
    fun key(key: String, pressed: Boolean)
}

/** How this device describes itself in hello. */
class Hello(val name: String, val platform: String, val screens: List<Map<String, Any>>)

enum class LinkState { WAITING, CONNECTING, CONNECTED }

/**
 * Applies the host's frames, like curslink.net.client.AgentSession: keeps
 * track of held keys and buttons so a leave or a dropped link lets go of them.
 */
class AgentSession(private val handler: InputHandler) {
    @Volatile var active = false
        private set
    private val keys = LinkedHashSet<String>()
    private val buttons = LinkedHashSet<String>()
    private var moveTs: Double? = null

    @Synchronized fun newConnection() { moveTs = null }

    @Synchronized private fun fresh(ts: Double?): Boolean {
        if (ts == null) return true
        val last = moveTs
        if (last != null && ts < last) return false
        moveTs = ts
        return true
    }

    fun udpMove(x: Double, y: Double, ts: Double) {
        if (active && fresh(ts)) handler.move(x, y)
    }

    /** A reply to send back, if the frame needs one. */
    fun handle(f: Frame): Pair<String, Map<String, JsonElement>>? {
        when (f.t) {
            "ping" -> return "pong" to LinkedHashMap<String, JsonElement>().apply {
                f["n"]?.let { put("n", it) }
                f["ts"]?.let { put("ts", it) }
            }
            "move" -> {
                val x = f.num("x") ?: return null
                val y = f.num("y") ?: return null
                if (fresh(f.num("ts"))) handler.move(x, y)
            }
            "enter" -> {
                active = true
                handler.enter(f.num("x") ?: 0.0, f.num("y") ?: 0.0, f.str("effect") ?: "none")
            }
            "leave" -> {
                releaseAll()
                handler.leave(f.str("effect") ?: "none", f.num("x"), f.num("y"))
            }
            "button" -> {
                val name = f.str("button") ?: return null
                val pressed = f.bool("pressed") ?: return null
                synchronized(this) {
                    if (!pressed && name !in buttons) return null
                    if (pressed) buttons.add(name) else buttons.remove(name)
                }
                handler.button(name, pressed)
            }
            "scroll" -> handler.scroll(f.num("dx") ?: 0.0, f.num("dy") ?: 0.0)
            "key" -> {
                val key = f.str("key")?.takeIf { it.isNotEmpty() } ?: return null
                val pressed = f.bool("pressed") ?: return null
                synchronized(this) {
                    if (!pressed && key !in keys) return null  // pressed elsewhere
                    if (pressed) keys.add(key) else keys.remove(key)
                }
                handler.key(key, pressed)
            }
            // Everything else (look, hint, drag, files, popups) is only sent to
            // agents that list the feature in hello, and we list none of them.
        }
        return null
    }

    fun releaseAll() {
        active = false
        val (k, b) = synchronized(this) {
            (keys.toList() to buttons.toList()).also { keys.clear(); buttons.clear() }
        }
        k.forEach { runCatching { handler.key(it, false) } }
        b.forEach { runCatching { handler.button(it, false) } }
    }
}

/**
 * Keeps this device linked to its host: straight over the LAN when it can
 * reach it (with moves by UDP), else through the relay, reconnecting with
 * backoff. Everything blocks, on its own thread.
 */
class AgentLink(
    handler: InputHandler,
    private val pairing: () -> HostPairing?,
    private val hello: () -> Hello,
    /** Extra (address, control port) candidates, best first: e.g. an mDNS lookup. */
    private val discover: (HostPairing) -> List<Pair<String, Int>> = { emptyList() },
    private val onState: (LinkState, String) -> Unit = { _, _ -> },
    private val log: (String) -> Unit = {},
) {
    val session = AgentSession(handler)
    @Volatile private var running = false
    @Volatile private var socket: Socket? = null
    @Volatile private var udp: AgentUdp? = null
    @Volatile var via: String = ""
        private set
    private var thread: Thread? = null
    private val wake = Object()

    val udpUp: Boolean get() = udp?.up == true

    fun start() {
        if (running) return
        running = true
        thread = Thread({ runForever() }, "curslink-link").apply { isDaemon = true; start() }
    }

    fun stop() {
        running = false
        reconnect()
        thread?.interrupt()
    }

    /** Drop the current connection (or skip the wait) and try again now:
     *  the network changed, the screen rotated, pairing changed. */
    fun reconnect() {
        runCatching { socket?.close() }
        synchronized(wake) { wake.notifyAll() }
    }

    private fun pause(ms: Long) {
        synchronized(wake) { if (running) wake.wait(ms) }
    }

    private fun runForever() {
        var backoff = 500L
        while (running) {
            val p = pairing()
            if (p == null) {
                onState(LinkState.WAITING, "not paired")
                pause(2000)
                continue
            }
            try {
                onState(LinkState.CONNECTING, "")
                runOnce(p)
                backoff = 500L
                log("disconnected from host; reconnecting")
            } catch (e: InterruptedException) {
                break
            } catch (e: Exception) {
                if (!running) break
                log("host unavailable: ${e.message ?: e.javaClass.simpleName}")
                onState(LinkState.WAITING, e.message ?: e.javaClass.simpleName)
            }
            pause(backoff)
            backoff = minOf(backoff * 2, 10_000L)
        }
    }

    private class Opened(val socket: Socket, val direct: Boolean, val via: String)

    private fun open(p: HostPairing): Opened {
        val candidates = LinkedHashSet<Pair<String, Int>>()
        runCatching { candidates.addAll(discover(p)) }
        p.addresses.forEach { candidates.add(it to p.controlPort) }
        val timeout = if (p.hasRelay) 2000 else 4000
        var last: Exception? = null
        for ((address, port) in candidates.take(6)) {
            val s = Socket()
            try {
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(address, port), timeout)
                return Opened(s, true, "$address:$port")
            } catch (e: Exception) {
                runCatching { s.close() }
                last = e
            }
        }
        if (p.hasRelay) {
            val s = Relay.connectAgent(p.relay, p.relayKey, Relay.pairedRoom(p.key, p.hostId))
            return Opened(s, false, "relay ${p.relay}")
        }
        throw last ?: java.io.IOException("no address for ${p.hostName}: pair again, or add a relay")
    }

    private fun runOnce(p: HostPairing) {
        val opened = open(p)
        val s = opened.socket
        socket = s
        if (!running) { s.close(); return }
        session.newConnection()
        try {
            s.soTimeout = 10_000
            val tls = Tls.connect(s.getInputStream(), s.getOutputStream(), p.myId, p.key)
            val io = FrameIO(tls.input, tls.output)
            val me = hello()
            val features = if (opened.direct) listOf("udp") else emptyList()
            io.send("hello", jsonOf(
                "v" to PROTOCOL_VERSION,
                "device_id" to p.myId,
                "name" to me.name,
                "platform" to me.platform,
                "screens" to me.screens,
                "features" to features,
                "auth" to Tls.helloAuth(p.key, tls.binding, p.myId),
            ))
            val welcome = io.read() ?: throw ProtocolException("the host closed the connection")
            if (welcome.t != "welcome") throw ProtocolException("host did not welcome us")
            via = opened.via
            log("connected to ${p.hostName} via $via")
            if (opened.direct) {
                AgentUdp.parseOffer(welcome.obj("udp"))?.let { offer ->
                    runCatching {
                        val u = AgentUdp(offer) { x, y, ts -> session.udpMove(x, y, ts) }
                        u.start(s.inetAddress.hostAddress)
                        udp = u
                    }.onFailure { log("pointer moves will come over TCP: ${it.message}") }
                }
            }
            onState(LinkState.CONNECTED, via)
            while (running) {
                val f = try {
                    io.read()
                } catch (e: SocketTimeoutException) {
                    throw java.io.IOException("the host stopped answering")
                } ?: break
                session.handle(f)?.let { (t, data) -> io.send(t, data) }
            }
        } finally {
            udp?.close()
            udp = null
            socket = null
            runCatching { s.close() }
            session.releaseAll()
        }
    }
}
