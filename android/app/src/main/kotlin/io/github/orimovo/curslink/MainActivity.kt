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
    // The link's state changes (a reconnect try every few seconds while the
    // computer can't be reached): only the status line follows it while a
    // text box is being typed in, or rebuilding the page would take the box
    // away from under the keyboard, which then flickers shut and open.
    private val refresh = { refreshed() }
    private var status: TextView? = null
    private var shown: List<Any?>? = null
    private var shownRelay: Pair<String, String>? = null

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
        render()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        // Android lets an app read the clipboard only while it has focus:
        // share anything copied in another app since we last could.
        if (hasFocus) LinkService.instance?.clipChanged()
    }

    override fun onPause() {
        LinkService.unlisten(refresh)
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

    /** What the page shows: when none of it changed, there's nothing to rebuild. */
    private fun looks(): List<Any?> = listOf(
        prefs.pairing, prefs.paused, prefs.pauseDuringCalls, prefs.keepScreenOn, LinkService.state,
        LinkService.instance?.pairRequest, busy, accessibilityOn(),
    )

    private fun refreshed() {
        if (isFinishing) return
        if (currentFocus is EditText) {
            status?.text = statusText()
            val request = LinkService.instance?.pairRequest
            if (request != null && pairDialog == null) askForCode(request) else if (request == null) pairDialog?.dismiss()
            return
        }
        if (looks() == shown) return
        render()
    }

    private fun statusText(): String {
        val pairing = prefs.pairing
        val (state, detail) = LinkService.state
        return when {
            pairing == null -> "Not paired with a computer yet."
            prefs.paused -> "Paused. ${pairing.hostName}'s mouse and keyboard won't come over to the phone until you resume."
            state == LinkState.PAUSED -> "Paused for your call. Curslink comes back on by itself when the call ends."
            state == LinkState.CONNECTED -> "Linked to ${pairing.hostName}" +
                (if (detail.startsWith("relay")) " through your relay." else " on this network.") +
                " Move the pointer past the phone's edge on the computer to use it here."
            else -> "Looking for ${pairing.hostName}…" + if (detail.isNotEmpty() && detail != "not paired") "\n$detail" else ""
        }
    }

    private fun render() {
        shown = looks()
        // Keep what's typed but not saved yet in the relay boxes (unless a
        // QR code or Save changed the relay since).
        val typed = if (prefs.relay == shownRelay) {
            (0 until root.childCount).mapNotNull { root.getChildAt(it) as? EditText }.map { it.text.toString() }
        } else emptyList()
        shownRelay = prefs.relay
        root.removeAllViews()
        root.addView(text("Curslink", 28f, bold = true, top = 0))
        val pairing = prefs.pairing
        status = text(statusText(), 16f).also { root.addView(it) }
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
        val address = EditText(this).apply { hint = "relay.example.com:24852"; setText(typed.getOrNull(0) ?: relay); setSingleLine() }
        val key = EditText(this).apply {
            hint = "Relay key"; setText(typed.getOrNull(1) ?: relayKey); setSingleLine()
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
        root.addView(text("Help", 20f, bold = true, top = 24))
        root.addView(text("If something isn't working, send these to whoever's helping you. They say what the phone tried and what the computer answered, never your keys.", 13f))
        root.addView(button("Export logs") { exportLogs() })
        root.addView(text("This phone: ${prefs.name} · id ${prefs.deviceId}", 12f, top = 24).apply { gravity = Gravity.START })

        val request = LinkService.instance?.pairRequest
        if (request != null && pairDialog == null) askForCode(request) else if (request == null) pairDialog?.dismiss()
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
