package io.github.orimovo.curslink.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import java.math.BigInteger

/**
 * Code-confirmed pairing, byte for byte as curslink/pairing/handshake.py:
 * ephemeral DH on the RFC 3526 2048-bit group, a commitment from the
 * initiator, a 6-digit code (or a QR token's proof) to rule out a man in
 * the middle, and a 32-byte link key both sides keep.
 */
data class DeviceInfo(val deviceId: String, val name: String, val role: String)

class PairResult(val peer: DeviceInfo, val key: ByteArray, val extra: JsonObject)

class PairingException(message: String) : Exception(message)

object Pairing {
    const val VERSION = 1
    const val KEY_BYTES = 32
    private const val PUB_BYTES = 256

    private val P = BigInteger(
        "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74" +
            "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437" +
            "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED" +
            "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05" +
            "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB" +
            "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B" +
            "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718" +
            "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF",
        16,
    )
    private val G = BigInteger.TWO
    private val Q = P.subtract(BigInteger.ONE).shiftRight(1)

    private class KeyPair(val x: BigInteger, val pub: ByteArray)

    private fun keypair(): KeyPair {
        val x = BigInteger(320, Crypto.random).setBit(319)
        return KeyPair(x, fixed(G.modPow(x, P)))
    }

    private fun fixed(v: BigInteger): ByteArray {
        val raw = v.toByteArray()
        return when {
            raw.size == PUB_BYTES -> raw
            raw.size > PUB_BYTES -> raw.copyOfRange(raw.size - PUB_BYTES, raw.size)
            else -> ByteArray(PUB_BYTES - raw.size) + raw
        }
    }

    private fun peerValue(raw: String?): BigInteger {
        val data = try {
            raw!!.unhex()
        } catch (e: Exception) {
            throw PairingException("malformed public value")
        }
        val y = BigInteger(1, data)
        if (data.size != PUB_BYTES || y <= BigInteger.ONE || y >= P.subtract(BigInteger.ONE) || y.modPow(Q, P) != BigInteger.ONE) {
            throw PairingException("invalid public value")
        }
        return y
    }

    private fun commit(pub: ByteArray) = Crypto.sha256("curslink pair commit\u0000".utf8(), pub).hex()

    private fun transcript(initiator: DeviceInfo, commit: String, recipient: DeviceInfo, pubR: ByteArray, pubI: ByteArray): ByteArray {
        val md = java.security.MessageDigest.getInstance("SHA-256")
        md.update("curslink pair v1\u0000".utf8())
        for (part in listOf(initiator.deviceId, initiator.name, initiator.role, commit, recipient.deviceId, recipient.name, recipient.role)) {
            val data = part.utf8()
            md.update(java.nio.ByteBuffer.allocate(4).putInt(data.size).array())
            md.update(data)
        }
        md.update(pubR)
        md.update(pubI)
        return md.digest()
    }

    private class Derived(val code: String, val key: ByteArray, val mac: String)

    private fun derive(x: BigInteger, peer: BigInteger, transcript: ByteArray): Derived {
        val shared = fixed(peer.modPow(x, P))
        val codeBytes = Crypto.hkdf(shared, transcript, "curslink code".utf8(), 8)
        val code = BigInteger(1, codeBytes).mod(BigInteger.valueOf(1_000_000)).toInt().toString().padStart(6, '0')
        val key = Crypto.hkdf(shared, transcript, "curslink link key".utf8(), KEY_BYTES)
        val mac = Crypto.hmac(key, "curslink pair confirm\u0000".utf8(), transcript).hex()
        return Derived(code, key, mac)
    }

    fun qrProof(token: ByteArray, transcript: ByteArray): String =
        Crypto.hmac(token, "curslink pair qr\u0000".utf8(), transcript).hex()

    fun formatCode(code: String) = code.take(3) + " " + code.drop(3)

    private fun read(io: FrameIO, expected: String): Frame {
        val msg = try {
            io.read()
        } catch (e: java.net.SocketTimeoutException) {
            throw PairingException("the other device stopped responding")
        } catch (e: ProtocolException) {
            throw PairingException("bad message: ${e.message}")
        } ?: throw PairingException("the other device closed the connection")
        if (msg.t == "pair_done" && expected != "pair_done") throw PairingException(msg.str("reason") ?: "refused")
        if (msg.t != expected) throw PairingException("expected $expected, got ${msg.t}")
        return msg
    }

    private fun info(msg: Frame): DeviceInfo {
        val id = msg.str("device_id")
        if (id.isNullOrEmpty() || id.length > 128) throw PairingException("missing device id")
        val role = msg.str("role")
        if (role != "host" && role != "agent") throw PairingException("unknown role")
        val name = msg.str("name")?.takeIf { it.isNotEmpty() } ?: id
        return DeviceInfo(id, name.take(128), role)
    }

    private fun checkRoles(me: DeviceInfo, peer: DeviceInfo) {
        if (peer.deviceId == me.deviceId) throw PairingException("both devices are called ${me.deviceId}")
        if (peer.role == me.role) throw PairingException("both devices are ${me.role}s; pair a host with an agent")
    }

    private fun extra(msg: Frame): JsonObject =
        JsonObject(msg.data.filterKeys { it == "screens" || it == "platform" })

    /**
     * Ask the device at the other end to pair (we scanned its QR code, so
     * [token] is set, or the user will type the [showCode] code into it).
     */
    fun initiate(
        io: FrameIO,
        me: DeviceInfo,
        token: ByteArray? = null,
        about: Map<String, JsonElement> = emptyMap(),
        showCode: (String, DeviceInfo) -> Unit = { _, _ -> },
    ): PairResult {
        val kp = keypair()
        val c = commit(kp.pub)
        val hello = LinkedHashMap(about)
        if (token != null) hello["qr"] = toJson(true)
        hello.putAll(jsonOf("v" to VERSION, "device_id" to me.deviceId, "name" to me.name, "role" to me.role, "commit" to c))
        io.send("pair_hello", hello)
        val msg = read(io, "pair_key")
        val peer = info(msg)
        checkRoles(me, peer)
        val y = peerValue(msg.str("pub"))
        val t = transcript(me, c, peer, fixed(y), kp.pub)
        val reveal = LinkedHashMap(jsonOf("pub" to kp.pub.hex()))
        if (token != null) reveal["proof"] = toJson(qrProof(token, t))
        io.send("pair_reveal", reveal)
        val d = derive(kp.x, y, t)
        showCode(formatCode(d.code), peer)
        val done = read(io, "pair_done")
        if (done.bool("ok") != true) throw PairingException(done.str("reason") ?: "refused")
        val mac = done.str("mac")
        if (mac == null || !Crypto.constantTimeEquals(mac, d.mac)) throw PairingException("the other device's confirmation did not match")
        return PairResult(peer, d.key, extra(msg))
    }

    /**
     * Answer a pairing request. [askCode] is called (on this thread) with the
     * device asking and returns what the user typed, or null to decline.
     */
    fun respond(
        io: FrameIO,
        me: DeviceInfo,
        about: Map<String, JsonElement> = emptyMap(),
        askCode: (DeviceInfo) -> String?,
    ): PairResult {
        fun refuse(reason: String) = runCatching { io.send("pair_done", jsonOf("ok" to false, "reason" to reason)) }

        val hello = read(io, "pair_hello")
        if (hello.num("v")?.toInt() != VERSION) {
            refuse("unsupported pairing version")
            throw PairingException("unsupported pairing version")
        }
        val peer = info(hello)
        try {
            checkRoles(me, peer)
        } catch (e: PairingException) {
            refuse(e.message ?: "refused")
            throw e
        }
        val c = hello.str("commit")
        if (c == null || c.length != 64) {
            refuse("missing commitment")
            throw PairingException("missing commitment")
        }
        if (hello.bool("qr") == true) {
            refuse("scan the QR code on the computer instead")
            throw PairingException("a QR request, but this phone shows no QR code")
        }
        val kp = keypair()
        val key = LinkedHashMap(about)
        key.putAll(jsonOf("device_id" to me.deviceId, "name" to me.name, "role" to me.role, "pub" to kp.pub.hex()))
        io.send("pair_key", key)
        val reveal = read(io, "pair_reveal")
        val y = peerValue(reveal.str("pub"))
        val pubI = fixed(y)
        if (!Crypto.constantTimeEquals(commit(pubI), c)) {
            refuse("commitment mismatch")
            throw PairingException("the other device's key did not match its commitment")
        }
        val t = transcript(peer, c, me, kp.pub, pubI)
        val d = derive(kp.x, y, t)
        val entered = askCode(peer)
        if (entered == null) {
            refuse("declined")
            throw PairingException("declined")
        }
        if (!Crypto.constantTimeEquals(entered.filter { it.isDigit() }, d.code)) {
            refuse("wrong code")
            throw PairingException("wrong code")
        }
        io.send("pair_done", jsonOf("ok" to true, "mac" to d.mac))
        return PairResult(peer, d.key, extra(hello))
    }
}
