package io.github.orimovo.curslink.core

import java.net.URI
import java.net.URLDecoder
import java.util.Base64

/** A host's pairing QR code (curslink/pairing/qr.py): curslink://pair?... */
data class Invite(
    val hostId: String,
    val hostName: String,
    val addresses: List<String>,
    val pairingPort: Int,
    val controlPort: Int,
    val token: ByteArray,
    val relay: String,
    val relayKey: String,
) {
    companion object {
        fun parse(text: String): Invite {
            val uri = try {
                URI(text.trim())
            } catch (e: Exception) {
                throw IllegalArgumentException("That isn't a Curslink pairing code.")
            }
            if (uri.scheme != "curslink" || uri.host != "pair") throw IllegalArgumentException("That isn't a Curslink pairing code.")
            val q = (uri.rawQuery ?: "").split("&").filter { it.contains("=") }.associate {
                URLDecoder.decode(it.substringBefore("="), "UTF-8") to URLDecoder.decode(it.substringAfter("="), "UTF-8")
            }
            val id = q["id"]
            val a = q["a"]
            val t = q["t"]
            if (q["v"] != "1" || id.isNullOrEmpty() || a.isNullOrEmpty() || t.isNullOrEmpty()) {
                throw IllegalArgumentException("This pairing code is from a newer Curslink. Update the app.")
            }
            return Invite(
                hostId = id,
                hostName = q["n"]?.ifEmpty { null } ?: id,
                addresses = a.split(",").filter { it.isNotBlank() },
                pairingPort = q["p"]?.toIntOrNull() ?: DEFAULT_PAIRING_PORT,
                controlPort = q["c"]?.toIntOrNull()?.takeIf { it > 0 } ?: DEFAULT_PORT,
                token = Base64.getUrlDecoder().decode(t),
                relay = q["r"] ?: "",
                relayKey = q["k"] ?: "",
            )
        }
    }
}
