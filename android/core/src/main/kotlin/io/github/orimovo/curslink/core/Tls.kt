package io.github.orimovo.curslink.core

import org.bouncycastle.tls.BasicTlsPSKIdentity
import org.bouncycastle.tls.ChannelBinding
import org.bouncycastle.tls.CipherSuite
import org.bouncycastle.tls.PSKTlsClient
import org.bouncycastle.tls.ProtocolVersion
import org.bouncycastle.tls.TlsClientProtocol
import org.bouncycastle.tls.crypto.impl.bc.BcTlsCrypto
import java.io.InputStream
import java.io.OutputStream
import java.security.SecureRandom

/**
 * The agent's end of the host's TLS-PSK link (curslink/security.py):
 * TLS 1.2, ECDHE-PSK suites only (forward secrecy), PSK identity
 * "curslink-v2:<our device id>" keyed with the pairing's link key.
 */
object Tls {
    const val PAIRED_IDENTITY_PREFIX = "curslink-v2:"

    private val SUITES = intArrayOf(
        CipherSuite.TLS_ECDHE_PSK_WITH_CHACHA20_POLY1305_SHA256,
        CipherSuite.TLS_ECDHE_PSK_WITH_AES_128_CBC_SHA256,
    )

    private class Client(identity: String, key: ByteArray) :
        PSKTlsClient(BcTlsCrypto(SecureRandom()), BasicTlsPSKIdentity(identity, key)) {
        override fun getSupportedCipherSuites(): IntArray = SUITES.clone()
        override fun getSupportedVersions(): Array<ProtocolVersion> = ProtocolVersion.TLSv12.only()
        fun tlsUnique(): ByteArray = context.exportChannelBinding(ChannelBinding.tls_unique)
    }

    class Session(val input: InputStream, val output: OutputStream, val binding: ByteArray, private val protocol: TlsClientProtocol) {
        fun close() = runCatching { protocol.close() }
    }

    /** Runs the handshake over [input]/[output] (a socket, or a relay pipe). */
    fun connect(input: InputStream, output: OutputStream, deviceId: String, key: ByteArray): Session {
        val client = Client(PAIRED_IDENTITY_PREFIX + deviceId, key)
        val protocol = TlsClientProtocol(input, output)
        protocol.connect(client)
        return Session(protocol.inputStream, protocol.outputStream, client.tlsUnique(), protocol)
    }

    /** hello.auth: proves which key we hold (curslink.security.hello_auth). */
    fun helloAuth(key: ByteArray, binding: ByteArray, deviceId: String): String =
        Crypto.hmac(key, "curslink hello\u0000".utf8(), binding, deviceId.utf8()).hex()
}
