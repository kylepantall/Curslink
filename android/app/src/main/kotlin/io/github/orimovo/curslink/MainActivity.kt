package io.github.orimovo.curslink

import android.Manifest
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SwitchCompat
import androidx.core.content.ContextCompat
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import io.github.orimovo.curslink.core.DeviceInfo
import io.github.orimovo.curslink.core.Invite
import io.github.orimovo.curslink.core.LinkState
import io.github.orimovo.curslink.core.PairingNet
import kotlin.concurrent.thread

/** Setup and status: switch on what Curslink needs, pair, and see the link. */
class MainActivity : AppCompatActivity() {
    private lateinit var prefs: Prefs
    private lateinit var root: LinearLayout
    private var pairDialog: AlertDialog? = null
    private var busy = false
    private val refresh = { render() }
    private var updateLine: TextView? = null
    private val updateChanged = { renderUpdate() }

    private val scan = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let { pairWithCode(it) }
    }

    private val askNotifications = registerForActivityResult(
        androidx.activity.result.contract.ActivityResultContracts.RequestPermission(),
    ) { render() }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(28), dp(20), dp(28))
        }
        setContentView(ScrollView(this).apply { addView(root) })
        LinkService.start(this)
    }

    override fun onResume() {
        super.onResume()
        LinkService.listen(refresh)
        Updater.listen(updateChanged)
        Updater.inFront = true
        render()
        Updater.check(this, maxAgeMs = 60 * 60 * 1000L)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // Android lets an app read the clipboard only while it has focus:
        // share anything copied in another app since we last could.
        if (hasFocus) LinkService.instance?.clipChanged()
    }

    override fun onPause() {
        LinkService.unlisten(refresh)
        Updater.unlisten(updateChanged)
        Updater.inFront = false
        super.onPause()
    }

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    private fun text(s: String, size: Float = 15f, bold: Boolean = false, top: Int = 6) = TextView(this).apply {
        text = s
        textSize = size
        if (bold) setTypeface(typeface, Typeface.BOLD)
        setPadding(0, dp(top), 0, dp(4))
    }

    private fun button(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    private fun step(done: Boolean, title: String, detail: String, action: String, onClick: () -> Unit) {
        root.addView(text((if (done) "✓  " else "○  ") + title, 16f, bold = true, top = 14))
        if (!done) {
            root.addView(text(detail))
            root.addView(button(action, onClick))
        }
    }

    private fun accessibilityOn(): Boolean {
        if (InputService.instance != null) return true
        val enabled = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES) ?: return false
        val me = ComponentName(this, InputService::class.java).flattenToString()
        return enabled.split(':').any { it.equals(me, ignoreCase = true) }
    }

    private fun render() {
        root.removeAllViews()
        root.addView(text("Curslink", 28f, bold = true, top = 0))
        val pairing = prefs.pairing
        val (state, detail) = LinkService.state
        val status = when {
            pairing == null -> "Not paired with a computer yet."
            prefs.paused -> "Paused. ${pairing.hostName}'s mouse and keyboard won't come over to the phone until you resume."
            state == LinkState.PAUSED -> "Paused for your call. Curslink comes back on by itself when the call ends."
            state == LinkState.CONNECTED -> "Linked to ${pairing.hostName}" +
                (if (detail.startsWith("relay")) " through your relay." else " on this network.") +
                " Move the pointer past the phone's edge on the computer to use it here."
            else -> "Looking for ${pairing.hostName}…" + if (detail.isNotEmpty() && detail != "not paired") "\n$detail" else ""
        }
        root.addView(text(status, 16f))
        if (pairing != null && state == LinkState.CONNECTED) {
            root.addView(button("Line up screens with the camera") {
                thread(name = "curslink-lineup") {
                    val asked = runCatching { LinkService.instance?.lineupRequest() == true }.getOrDefault(false)
                    runOnUiThread {
                        Toast.makeText(this,
                            if (asked) "Every screen shows its patterns now; the camera opens in a moment."
                            else "${pairing.hostName}'s Curslink is too old to line up screens. Update it, or use Layout › Line up with camera there.",
                            Toast.LENGTH_LONG).show()
                    }
                }
            })
        }
        if (pairing != null) {
            root.addView(button(if (prefs.paused) "Resume" else "Pause") {
                LinkService.setPaused(this, !prefs.paused)
                render()
            })
            root.addView(CheckBox(this).apply {
                text = "Pause during calls"
                isChecked = prefs.pauseDuringCalls
                setOnCheckedChangeListener { _, on ->
                    prefs.pauseDuringCalls = on
                    LinkService.instance?.applyPause()
                }
            })
            root.addView(text("Also on the notification, and as a Quick Settings tile: edit your tiles and drag Curslink in.", 13f))
        }

        root.addView(text("Set up", 20f, bold = true, top = 24))
        step(accessibilityOn(), "Mouse and keyboard",
            "Turn on \"Curslink mouse and keyboard\" in Accessibility. It draws the pointer, turns clicks and scrolls into touches, and types for you.",
            "Open Accessibility settings") {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
        if (Build.VERSION.SDK_INT >= 33) {
            val granted = ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            step(granted, "Notifications", "Lets Curslink show that it's linked, and ask you for pairing codes.", "Allow") {
                askNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }
        val pm = getSystemService(POWER_SERVICE) as PowerManager
        step(pm.isIgnoringBatteryOptimizations(packageName), "Stay linked while the phone sleeps",
            "So the phone wakes up when the mouse comes over, even after it's been locked a while.", "Allow") {
            runCatching {
                startActivity(Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS, Uri.parse("package:$packageName")))
            }
        }

        root.addView(text("Computer", 20f, bold = true, top = 24))
        if (pairing != null) {
            root.addView(text("Paired with ${pairing.hostName}."))
            root.addView(button("Scan a new QR code") { startScan() })
            root.addView(button("Unpair") {
                AlertDialog.Builder(this).setMessage("Forget ${pairing.hostName}? Unpair the phone on the computer too.")
                    .setPositiveButton("Unpair") { _, _ -> LinkService.instance?.unpair() ?: run { prefs.pairing = null }; render() }
                    .setNegativeButton("Cancel", null).show()
            })
        } else {
            root.addView(text("On your computer, open Curslink › Devices and choose Pair a phone, then scan the code it shows."))
            root.addView(button(if (busy) "Pairing…" else "Scan QR code") { if (!busy) startScan() })
            root.addView(text("Or pick \"${prefs.name}\" under Nearby on the computer's Devices page, and type the code it shows here.", 13f))
        }

        root.addView(text("Screen", 20f, bold = true, top = 24))
        root.addView(SwitchCompat(this).apply {
            text = "Keep the screen on"
            textSize = 16f
            isChecked = prefs.keepScreenOn
            setOnCheckedChangeListener { _, on ->
                prefs.keepScreenOn = on
                InputService.instance?.holdScreen(on && LinkService.state.first == LinkState.CONNECTED)
            }
        })
        root.addView(text("While the phone is linked to ${pairing?.hostName ?: "your computer"}, its screen stays on " +
            "instead of dimming and locking, so it's ready when the pointer comes over. It sleeps as usual once the " +
            "link drops. Without this it stays on for 2 minutes after you use the mouse here.", 13f))

        root.addView(text("Away from home", 20f, bold = true, top = 24))
        root.addView(text("With a Curslink relay, the phone can reach the computer from any network. A QR code from a computer that uses one fills this in.", 13f))
        val (relay, relayKey) = prefs.relay
        val address = EditText(this).apply { hint = "relay.example.com:24852"; setText(relay); setSingleLine() }
        val key = EditText(this).apply {
            hint = "Relay key"; setText(relayKey); setSingleLine()
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        root.addView(address)
        root.addView(key)
        root.addView(button("Save relay") {
            prefs.relay = address.text.toString() to key.text.toString()
            prefs.pairing?.let { prefs.pairing = it.copy(relay = prefs.relay.first, relayKey = prefs.relay.second) }
            LinkService.instance?.relink()
            Toast.makeText(this, "Saved", Toast.LENGTH_SHORT).show()
        })
        renderUpdates()

        root.addView(text("Help", 20f, bold = true, top = 24))
        root.addView(text("If something isn't working, send these to whoever's helping you. They say what the phone tried and what the computer answered, never your keys.", 13f))
        root.addView(button("Export logs") { exportLogs() })
        root.addView(text("This phone: ${prefs.name} · id ${prefs.deviceId}", 12f, top = 24).apply { gravity = Gravity.START })

        val request = LinkService.instance?.pairRequest
        if (request != null && pairDialog == null) askForCode(request) else if (request == null) pairDialog?.dismiss()
    }

    private var updateButton: Button? = null

    private fun renderUpdates() {
        root.addView(text("Updates", 20f, bold = true, top = 24))
        updateLine = text("", 15f).also { root.addView(it) }
        updateButton = button("") {
            if (Updater.release != null) Updater.updateNow(this) else Updater.check(this, 0, byHand = true)
        }.also { root.addView(it) }
        renderUpdate()
        root.addView(SwitchCompat(this).apply {
            text = "Update automatically"
            textSize = 16f
            isChecked = prefs.autoUpdate
            setOnCheckedChangeListener { _, on ->
                prefs.autoUpdate = on
                if (on) Updater.check(this@MainActivity, 0)
            }
        })
        root.addView(text("Curslink looks for a new release a few times a day, downloads it on Wi-Fi and installs it " +
            "while the screen is off. Android asks you to confirm the first update; after that they go in by themselves.", 13f))
        if (!packageManager.canRequestPackageInstalls()) {
            step(false, "Allow updates", "Android needs your OK for Curslink to install its own updates: turn on \"Allow from this source\".",
                "Open settings") {
                runCatching { startActivity(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName"))) }
            }
        }
    }

    /** Just the update line and button, so a download's progress doesn't rebuild the page. */
    private fun renderUpdate() {
        val line = updateLine ?: return
        val r = Updater.release
        val version = Updater.currentVersion(this)
        val checked = maxOf(Updater.lastChecked, prefs.updateCheckedAt)
        line.text = when {
            Updater.message.isNotEmpty() -> "This is $version. ${Updater.message}"
            r != null -> "This is $version. Curslink ${r.version} is out."
            checked > 0 -> "This is $version, the newest. Checked " +
                android.text.format.DateUtils.getRelativeTimeSpanString(checked) + "."
            else -> "This is $version."
        }
        updateButton?.apply {
            val working = Updater.phase in setOf(Updater.Phase.CHECKING, Updater.Phase.DOWNLOADING, Updater.Phase.INSTALLING)
            isEnabled = !working
            text = if (r != null) "Update to ${r.version}" else "Check for updates"
        }
    }

    private fun exportLogs() {
        val pairing = prefs.pairing
        val (state, detail) = LinkService.state
        val summary = buildString {
            append("This phone: ${prefs.name} (${prefs.deviceId})\n")
            append(pairing?.let { "Paired with: ${it.hostName} (${it.hostId}), addresses ${it.addresses.joinToString().ifEmpty { "none" }}, port ${it.controlPort}" +
                (if (it.hasRelay) ", relay ${it.relay}" else ", no relay") } ?: "Not paired")
            append("\nPaused: ${if (prefs.paused) "yes" else "no"}, pause during calls: ${if (prefs.pauseDuringCalls) "on" else "off"}")
            append("\nLink: $state${if (detail.isNotEmpty()) " ($detail)" else ""}")
            append("\nMouse and keyboard (accessibility): ${if (accessibilityOn()) "on" else "off"}")
            append("\nKeep the screen on: ${if (prefs.keepScreenOn) "on" else "off"}")
            append("\nIgnoring battery optimisation: ${(getSystemService(POWER_SERVICE) as PowerManager).isIgnoringBatteryOptimizations(packageName)}")
            append("\nApp version: ${Updater.currentVersion(this@MainActivity)}, update automatically: ${if (prefs.autoUpdate) "on" else "off"}")
            append("\nScreen layout size: ${prefs.layoutSize?.let { "${it.first} x ${it.second} dp" } ?: "not set"}")
        }
        runCatching { AppLog.share(this, summary) }.onFailure {
            AppLog.w("CurslinkMain", "couldn't export logs", it)
            Toast.makeText(this, "Couldn't export the logs: ${it.message}", Toast.LENGTH_LONG).show()
        }
    }

    private fun startScan() {
        scan.launch(ScanOptions().apply {
            setDesiredBarcodeFormats(ScanOptions.QR_CODE)
            setPrompt("Scan the QR code on your computer's Devices page")
            setBeepEnabled(false)
            setOrientationLocked(false)
        })
    }

    private fun pairWithCode(text: String) {
        val invite = try {
            Invite.parse(text)
        } catch (e: IllegalArgumentException) {
            Toast.makeText(this, e.message, Toast.LENGTH_LONG).show()
            return
        }
        val service = LinkService.instance ?: return
        busy = true
        render()
        val me = DeviceInfo(prefs.deviceId, prefs.name, "agent")
        thread(name = "curslink-qr-pair") {
            val outcome = runCatching { PairingNet.pairByQr(invite, me, service.about()) }
            runOnUiThread {
                busy = false
                outcome.onFailure { AppLog.w("CurslinkMain", "QR pairing with ${invite.hostName} failed", it) }
                outcome.onSuccess { (result, address) ->
                    service.paired(result, address, invite.controlPort, invite.relay to invite.relayKey)
                    Toast.makeText(this, "Paired with ${result.peer.name}", Toast.LENGTH_LONG).show()
                }.onFailure {
                    AlertDialog.Builder(this).setTitle("Couldn't pair").setMessage(it.message ?: it.toString())
                        .setPositiveButton("OK", null).show()
                }
                render()
            }
        }
    }

    private fun askForCode(request: LinkService.PairRequest) {
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            hint = "123 456"
            textSize = 24f
            gravity = Gravity.CENTER
        }
        pairDialog = AlertDialog.Builder(this)
            .setTitle("Pair with ${request.peer.name}?")
            .setMessage("Type the 6-digit code ${request.peer.name} is showing.")
            .setView(input)
            .setPositiveButton("Pair") { _, _ -> request.answer.complete(input.text.toString()) }
            .setNegativeButton("Decline") { _, _ -> request.answer.complete(null) }
            .setOnDismissListener { pairDialog = null; if (!request.answer.isDone) request.answer.complete(null) }
            .show()
        input.requestFocus()
        input.visibility = View.VISIBLE
    }
}
