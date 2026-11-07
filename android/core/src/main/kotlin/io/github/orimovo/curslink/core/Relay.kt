package io.github.orimovo.curslink.core

import java.net.InetSocketAddress
import java.net.Socket

/**
 * Meeting the host at a self-hosted relay (curslink/net/relay.py). The relay
 * only pipes bytes: TLS-PSK runs end to end through it, so it never sees a
 * keystroke. The room is a one-way hash of the link key and the host's id.
 */
object Relay {
    class RelayException(message: String) : java.io.IOException(message)

    private const val VERSION = 1
    private const val TIMEOUT_MS = 10_000
    private const val PAIR_TIMEOUT_MS = 30_000

    fun pairedRoom(linkKey: ByteArray, hostId: String): String =
        Crypto.hmac(linkKey, "curslink/v2/relay-room\u0000".utf8(), hostId.utf8()).hex()

    private fun proof(key: String, label: String, vararg parts: String): String =
        Crypto.hmac(key.utf8(), (label + "|" + parts.joinToString("|")).utf8()).hex()

    fun parseAddress(address: String): Pair<String, Int> {
        val a = address.trim()
        require(a.isNotEmpty()) { "relay address is empty" }
        return when {
            a.startsWith("[") -> {
                val host = a.substring(1, a.indexOf(']'))
                val port = a.substring(a.indexOf(']') + 1).removePrefix(":").ifEmpty { "$DEFAULT_RELAY_PORT" }
                host to port.toInt()
            }
            a.count { it == ':' } == 1 -> a.substringBefore(':') to a.substringAfter(':').toInt()
            else -> a to DEFAULT_RELAY_PORT
        }
    }

    /** A socket at the relay, paired with the waiting host: start TLS on it now. */
    fun connectAgent(address: String, relayKey: String, room: String): Socket {
        val (host, port) = parseAddress(address)
        val socket = Socket()
        try {
            socket.tcpNoDelay = true
            socket.connect(InetSocketAddress(host, port), TIMEOUT_MS)
            socket.soTimeout = TIMEOUT_MS
            val reader = LineReader(socket.getInputStream())
            val out = socket.getOutputStream()
            fun line(vararg pairs: Pair<String, Any?>) {
                out.write((kotlinx.serialization.json.JsonObject(jsonOf(*pairs)).toString() + "\n").utf8())
                out.flush()
            }
            fun next(): Frame = reader.readFrame() ?: throw RelayException("the relay closed the connection")

            val nonce = Crypto.randomBytes(16).hex()
            line("t" to "relay_hello", "v" to VERSION, "role" to "agent", "room" to room, "nonce" to nonce)
            val challenge = next()
            if (challenge.t == "relay_error") throw RelayException(challenge.str("reason") ?: "refused")
            if (challenge.t != "relay_challenge") throw RelayException("unexpected reply from relay")
            val serverNonce = challenge.str("nonce")?.takeIf { it.length == 32 } ?: throw RelayException("bad nonce")
            if (!Crypto.constantTimeEquals(challenge.str("proof") ?: "", proof(relayKey, "curslink/v1/relay-proof", nonce, serverNonce))) {
                throw RelayException("the relay couldn't prove it knows the relay key (wrong key, or not your relay)")
            }
            line("t" to "relay_auth", "proof" to proof(relayKey, "curslink/v1/client-proof", serverNonce, nonce, "agent", room))
            val ok = next()
            if (ok.t != "relay_ok") throw RelayException(ok.str("reason") ?: "relay refused the connection")
            socket.soTimeout = PAIR_TIMEOUT_MS
            val paired = next()
            if (paired.t == "relay_error") throw RelayException(paired.str("reason") ?: "refused")
            if (paired.t != "relay_paired") throw RelayException("unexpected ${paired.t} from relay")
            socket.soTimeout = 0
            return socket
        } catch (e: Exception) {
            runCatching { socket.close() }
            throw e
        }
    }
}
