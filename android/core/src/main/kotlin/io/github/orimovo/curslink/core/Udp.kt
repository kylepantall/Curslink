package io.github.orimovo.curslink.core

import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.nio.ByteBuffer
import java.util.Base64

/**
 * Pointer moves by UDP beside the TLS link (curslink/net/udp.py): a lost
 * move is skipped instead of holding up every move behind it.
 *
 * Datagram: "CL", version, kind, connection id (8), sequence (u64), the
 * body XORed with HMAC(enc, kind + seq), then a 16-byte HMAC tag.
 */
class UdpOffer(val port: Int, val id: ByteArray, val key: ByteArray, val address: String? = null)

class AgentUdp(
    offer: UdpOffer,
    /** True while input is on this phone (between enter and leave). */
    private val active: () -> Boolean = { false },
    private val onMove: (Double, Double, Double) -> Unit,
) {
    companion object {
        private const val VERSION = 1
        const val PROBE = 1
        const val ACK = 2
        const val MOVE = 3
        private const val HEADER = 20
        private const val TAG = 16
        private const val PROBE_EVERY_MS = 500L
        // While input is here and for WARM_AFTER_MS after, probe this often:
        // a phone's Wi-Fi dozes between packets, and a steady trickle keeps
        // it awake so the next moves aren't held at the access point
        // (PROBE_WARM_S in curslink/net/udp.py).
        internal const val PROBE_WARM_MS = 40L
        internal const val WARM_AFTER_MS = 5000L
        private const val ALIVE_MS = 1600L
        private fun bodySize(kind: Int) = when (kind) { PROBE -> 1; ACK -> 8; MOVE -> 24; else -> -1 }

        /** An IPv4 literal that isn't unspecified, loopback or multicast. */
        internal fun probeable(address: String): Boolean {
            val parts = address.split(".").map { p -> p.toIntOrNull()?.takeIf { p.length <= 3 && it in 0..255 } ?: return false }
            return parts.size == 4 && parts[0] != 0 && parts[0] != 127 && parts[0] < 224
        }

        fun parseOffer(udp: kotlinx.serialization.json.JsonObject?): UdpOffer? {
            udp ?: return null
            val id = udp["id"]?.strOrNull()?.let { runCatching { Base64.getDecoder().decode(it) }.getOrNull() }
            val key = udp["key"]?.strOrNull()?.let { runCatching { Base64.getDecoder().decode(it) }.getOrNull() }
            val port = udp["port"].numOrNull()?.toInt()
            if (id == null || id.size != 8 || key == null || key.size != 32 || port == null || port !in 1..65535) return null
            return UdpOffer(port, id, key, udp["address"]?.strOrNull()?.takeIf(::probeable))
        }
    }

    private val connId = offer.id
    private val enc = Crypto.hmac(offer.key, "curslink-udp-enc".utf8())
    private val mac = Crypto.hmac(offer.key, "curslink-udp-mac".utf8())
    private val port = offer.port
    private val address = offer.address  // the host's reply address, if not the one we dialled
    private var socket: DatagramSocket? = null
    @Volatile private var ackedAt = 0L
    @Volatile private var warmUntil = 0L
    private val kick = java.util.concurrent.Semaphore(0)
    private var seqOut = 0L
    private var seqIn = 0L
    @Volatile var moves = 0L
        private set
    private var threads = listOf<Thread>()

    val up: Boolean get() = System.currentTimeMillis() - ackedAt < ALIVE_MS

    private fun xor(kind: Int, seq: Long, data: ByteArray): ByteArray {
        val stream = Crypto.hmac(enc, byteArrayOf(kind.toByte()), ByteBuffer.allocate(8).putLong(seq).array())
        return ByteArray(data.size) { (data[it].toInt() xor stream[it].toInt()).toByte() }
    }

    fun seal(kind: Int, seq: Long, body: ByteArray): ByteArray {
        val head = ByteBuffer.allocate(HEADER).put('C'.code.toByte()).put('L'.code.toByte())
            .put(VERSION.toByte()).put(kind.toByte()).put(connId).putLong(seq).array()
        val sealed = head + xor(kind, seq, body)
        return sealed + Crypto.hmac(mac, sealed).copyOf(TAG)
    }

    /** (kind, seq, body) of a genuine datagram for this connection, else null. */
    fun open(packet: ByteArray): Triple<Int, Long, ByteArray>? {
        if (packet.size < HEADER + TAG || packet[0] != 'C'.code.toByte() || packet[1] != 'L'.code.toByte() || packet[2].toInt() != VERSION) return null
        if (!packet.copyOfRange(4, 12).contentEquals(connId)) return null
        val kind = packet[3].toInt()
        val body = packet.copyOfRange(HEADER, packet.size - TAG)
        if (body.size != bodySize(kind)) return null
        val want = Crypto.hmac(mac, packet.copyOfRange(0, packet.size - TAG)).copyOf(TAG)
        if (!java.security.MessageDigest.isEqual(want, packet.copyOfRange(packet.size - TAG, packet.size))) return null
        val seq = ByteBuffer.wrap(packet, 12, 8).long
        return Triple(kind, seq, xor(kind, seq, body))
    }

    fun start(hostIp: String) {
        val s = DatagramSocket()
        s.connect(InetSocketAddress(address ?: hostIp, port))
        socket = s
        val prober = Thread({
            try {
                while (!Thread.currentThread().isInterrupted) {
                    probe()
                    kick.tryAcquire(if (warming()) PROBE_WARM_MS else PROBE_EVERY_MS, java.util.concurrent.TimeUnit.MILLISECONDS)
                    kick.drainPermits()
                }
            } catch (_: InterruptedException) {
            }
        }, "curslink-udp-probe")
        val receiver = Thread({ receive(s) }, "curslink-udp")
        prober.isDaemon = true
        receiver.isDaemon = true
        threads = listOf(prober, receiver)
        threads.forEach { it.start() }
    }

    internal fun warming(): Boolean = active() || System.currentTimeMillis() < warmUntil

    /** Input is here: probe fast for a while (don't wait out a slow interval). */
    private fun warm() {
        val was = warming()
        warmUntil = System.currentTimeMillis() + WARM_AFTER_MS
        if (!was) kick.release()
    }

    @Synchronized
    private fun probe() {
        val s = socket ?: return
        seqOut++
        val data = seal(PROBE, seqOut, byteArrayOf(if (up) 1 else 0))
        runCatching { s.send(DatagramPacket(data, data.size)) }
    }

    private fun receive(s: DatagramSocket) {
        val buf = ByteArray(256)
        while (!s.isClosed) {
            val packet = DatagramPacket(buf, buf.size)
            try {
                s.receive(packet)
            } catch (_: Exception) {
                if (s.isClosed) return
                continue  // ICMP unreachable before the host listens, etc.
            }
            val (kind, seq, body) = open(buf.copyOf(packet.length)) ?: continue
            if (seq <= seqIn) continue  // late or replayed
            seqIn = seq
            when (kind) {
                ACK -> {
                    val first = !up
                    ackedAt = System.currentTimeMillis()
                    if (first) probe()  // tell the host straight away
                }
                MOVE -> {
                    ackedAt = System.currentTimeMillis()  // the host's datagrams get here: as good as an ACK
                    warm()
                    val b = ByteBuffer.wrap(body)
                    moves++
                    onMove(b.double, b.double, b.double)
                }
            }
        }
    }

    fun close() {
        threads.forEach { it.interrupt() }
        socket?.close()
        socket = null
    }
}
