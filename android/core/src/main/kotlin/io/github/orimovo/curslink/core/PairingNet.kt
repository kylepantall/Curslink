package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket

/** Pairing over the network: scanning a host's QR code, or answering a host
 *  that picked this phone from its Nearby list. */
object PairingNet {
    private const val STEP_TIMEOUT_MS = 15_000

    /** Pair with the host whose QR code we scanned. Tries its addresses in turn. */
    fun pairByQr(invite: Invite, me: DeviceInfo, about: Map<String, JsonElement>): Pair<PairResult, String> {
        var last: Exception? = null
        for (address in invite.addresses) {
            val s = Socket()
            try {
                s.connect(InetSocketAddress(address, invite.pairingPort), 3000)
                s.soTimeout = STEP_TIMEOUT_MS
                val io = FrameIO(s.getInputStream(), s.getOutputStream())
                val result = Pairing.initiate(io, me, invite.token, about)
                if (result.peer.deviceId != invite.hostId) throw PairingException("a different computer answered")
                return result to address
            } catch (e: PairingException) {
                throw e
            } catch (e: Exception) {
                last = e
            } finally {
                runCatching { s.close() }
            }
        }
        throw PairingException("Couldn't reach ${invite.hostName} (${last?.message ?: "no address"}). Is the phone on the same Wi-Fi?")
    }
}

/**
 * Listens for a host asking to pair (it found us by mDNS). [askCode] runs
 * on the connection's thread and blocks until the user types the code shown
 * on the computer, or declines (null).
 */
class PairingServer(
    private val me: () -> DeviceInfo,
    private val about: () -> Map<String, JsonElement>,
    private val askCode: (DeviceInfo) -> String?,
    private val onPaired: (PairResult, String) -> Unit,
    private val onFailed: (String) -> Unit = {},
    private val port: Int = DEFAULT_PAIRING_PORT,
) {
    private var server: ServerSocket? = null
    @Volatile private var busy = false
    private val failures = ArrayDeque<Long>()
    private var lockedUntil = 0L

    /** The port we listen on (any free one if [port] is taken). */
    val boundPort: Int get() = server?.localPort ?: 0

    fun start() {
        val ss = try {
            ServerSocket().apply { reuseAddress = true; bind(InetSocketAddress(port)) }
        } catch (e: Exception) {
            ServerSocket(0)
        }
        server = ss
        Thread({
            while (!ss.isClosed) {
                val s = try { ss.accept() } catch (e: Exception) { break }
                Thread({ handle(s) }, "curslink-pair-in").apply { isDaemon = true; start() }
            }
        }, "curslink-pairing").apply { isDaemon = true; start() }
    }

    fun close() {
        runCatching { server?.close() }
        server = null
    }

    private fun handle(s: Socket) {
        val address = s.inetAddress?.hostAddress ?: ""
        try {
            s.soTimeout = STEP_TIMEOUT_MS
            val io = FrameIO(s.getInputStream(), s.getOutputStream())
            val now = System.currentTimeMillis()
            val refusal = when {
                now < lockedUntil -> "too many failed attempts; try again in a minute"
                busy -> "already pairing with another device"
                else -> null
            }
            if (refusal != null) {
                io.send("pair_done", jsonOf("ok" to false, "reason" to refusal))
                return
            }
            busy = true
            try {
                // The code can take a while to type: no read is pending meanwhile.
                val result = Pairing.respond(io, me(), about(), askCode)
                onPaired(result, address)
            } catch (e: PairingException) {
                if (e.message != "declined") failed()
                onFailed(e.message ?: "pairing failed")
            } finally {
                busy = false
            }
        } catch (e: Exception) {
            onFailed(e.message ?: "pairing failed")
        } finally {
            runCatching { s.close() }
        }
    }

    @Synchronized private fun failed() {
        val now = System.currentTimeMillis()
        while (failures.isNotEmpty() && now - failures.first() > 300_000) failures.removeFirst()
        failures.addLast(now)
        if (failures.size >= 3) {
            lockedUntil = now + 60_000
            failures.clear()
        }
    }

    private companion object { const val STEP_TIMEOUT_MS = 15_000 }
}
