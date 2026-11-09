package io.github.orimovo.curslink

import android.content.Context
import android.content.SharedPreferences
import android.os.Build
import android.provider.Settings
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import io.github.orimovo.curslink.core.DEFAULT_PORT
import io.github.orimovo.curslink.core.HostPairing
import java.security.KeyStore
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * This phone's identity and its pairing with one computer. The link key is
 * kept encrypted with a key that never leaves the Android Keystore.
 */
class Prefs(context: Context) {
    private val app = context.applicationContext
    private val p: SharedPreferences = app.getSharedPreferences("curslink", Context.MODE_PRIVATE)

    val deviceId: String
        get() = p.getString("device_id", null) ?: ("android-" + ByteArray(4).also { SecureRandom().nextBytes(it) }
            .joinToString("") { "%02x".format(it) }).also { p.edit().putString("device_id", it).apply() }

    var name: String
        get() = p.getString("name", null) ?: defaultName()
        set(value) = p.edit().putString("name", value.trim().ifEmpty { defaultName() }).apply()

    private fun defaultName(): String =
        Settings.Global.getString(app.contentResolver, "device_name")?.takeIf { it.isNotBlank() }
            ?: "${Build.MANUFACTURER.replaceFirstChar { it.uppercase() }} ${Build.MODEL}"

    /** The screen size (dp) the computer laid this phone out with. */
    var layoutSize: Pair<Int, Int>?
        get() {
            val w = p.getInt("layout_w", 0)
            val h = p.getInt("layout_h", 0)
            return if (w > 0 && h > 0) w to h else null
        }
        set(value) = p.edit().putInt("layout_w", value?.first ?: 0).putInt("layout_h", value?.second ?: 0).apply()

    var pairing: HostPairing?
        get() {
            val hostId = p.getString("host_id", null) ?: return null
            val key = p.getString("host_key", null)?.let { unwrap(it) } ?: return null
            return HostPairing(
                hostId = hostId,
                hostName = p.getString("host_name", null) ?: hostId,
                myId = deviceId,
                key = key,
                addresses = p.getString("host_addresses", "")!!.split(",").filter { it.isNotBlank() },
                controlPort = p.getInt("host_port", DEFAULT_PORT),
                relay = p.getString("relay", "") ?: "",
                relayKey = p.getString("relay_key", "") ?: "",
            )
        }
        set(value) {
            val e = p.edit()
            if (value == null) {
                for (k in listOf("host_id", "host_key", "host_name", "host_addresses", "host_port")) e.remove(k)
            } else {
                e.putString("host_id", value.hostId)
                    .putString("host_key", wrap(value.key))
                    .putString("host_name", value.hostName)
                    .putString("host_addresses", value.addresses.joinToString(","))
                    .putInt("host_port", value.controlPort)
                    .putString("relay", value.relay)
                    .putString("relay_key", value.relayKey)
            }
            e.apply()
        }

    /** Paused by hand (the app, the notification or the Quick Settings
     *  tile): off the computer until resumed, even after a restart. */
    var paused: Boolean
        get() = p.getBoolean("paused", false)
        set(value) = p.edit().putBoolean("paused", value).apply()

    /** Pause by itself while a call (phone or app) is on, and come back after. */
    var pauseDuringCalls: Boolean
        get() = p.getBoolean("pause_during_calls", true)
        set(value) = p.edit().putBoolean("pause_during_calls", value).apply()

    var relay: Pair<String, String>
        get() = (p.getString("relay", "") ?: "") to (p.getString("relay_key", "") ?: "")
        set(value) = p.edit().putString("relay", value.first.trim()).putString("relay_key", value.second.trim()).apply()

    /** Remember an address that worked, first in line next time. */
    fun rememberAddress(address: String) {
        val now = pairing ?: return
        val list = (listOf(address) + now.addresses).distinct().take(4)
        if (list != now.addresses) p.edit().putString("host_addresses", list.joinToString(",")).apply()
    }

    // ---------------------------------------------------------- keystore
    private fun secret(): SecretKey {
        val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (ks.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val gen = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        gen.init(
            KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return gen.generateKey()
    }

    private fun wrap(raw: ByteArray): String {
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.ENCRYPT_MODE, secret())
        return Base64.encodeToString(c.iv + c.doFinal(raw), Base64.NO_WRAP)
    }

    private fun unwrap(text: String): ByteArray? = runCatching {
        val all = Base64.decode(text, Base64.NO_WRAP)
        val c = Cipher.getInstance("AES/GCM/NoPadding")
        c.init(Cipher.DECRYPT_MODE, secret(), GCMParameterSpec(128, all.copyOfRange(0, 12)))
        c.doFinal(all.copyOfRange(12, all.size))
    }.getOrNull()

    private companion object {
        const val ALIAS = "curslink-link-key"
    }
}
