package io.github.orimovo.curslink

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.Network
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import io.github.orimovo.curslink.core.AgentLink
import io.github.orimovo.curslink.core.DeviceInfo
import io.github.orimovo.curslink.core.Hello
import io.github.orimovo.curslink.core.HostPairing
import io.github.orimovo.curslink.core.InputHandler
import io.github.orimovo.curslink.core.LinkState
import io.github.orimovo.curslink.core.MirrorCapture
import io.github.orimovo.curslink.core.MirrorHandler
import io.github.orimovo.curslink.core.MirrorShow
import io.github.orimovo.curslink.core.PairResult
import io.github.orimovo.curslink.core.PairingServer
import io.github.orimovo.curslink.core.jsonOf
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit

/**
 * Keeps the phone linked to its computer while the screen is off, so moving
 * the pointer over (or looking at the phone) can wake it at any moment.
 * Also advertises the phone for pairing and answers the computer's request.
 */
class LinkService : Service() {
    companion object {
        private const val TAG = "CurslinkLink"
        const val CHANNEL_LINK = "link"
        const val CHANNEL_PAIR = "pair"
        const val CHANNEL_FILES = "files"
        private const val NOTE_LINK = 1
        private const val NOTE_PAIR = 2

        @Volatile var instance: LinkService? = null
            private set

        /** (state, detail) for the app's window. */
        @Volatile var state: Pair<LinkState, String> = LinkState.WAITING to "starting"
            private set
        private val listeners = mutableSetOf<() -> Unit>()
        fun listen(l: () -> Unit) = synchronized(listeners) { listeners.add(l) }
        fun unlisten(l: () -> Unit) = synchronized(listeners) { listeners.remove(l) }
        private fun changed() {
            val ls = synchronized(listeners) { listeners.toList() }
            Handler(Looper.getMainLooper()).post { ls.forEach { it() } }
        }

        fun start(context: Context) {
            ContextCompat.startForegroundService(context, Intent(context, LinkService::class.java))
        }

        /** Pause (off the computer until resumed) or resume, from anywhere. */
        fun setPaused(context: Context, on: Boolean) {
            Prefs(context).paused = on
            instance?.applyPause() ?: runCatching { start(context) }
            PauseTile.refresh(context)
            changed()
        }
    }

    /** A computer asking to pair: the app's window asks for the code. */
    class PairRequest(val peer: DeviceInfo, val answer: CompletableFuture<String?>)

    @Volatile var pairRequest: PairRequest? = null
        private set

    lateinit var prefs: Prefs
        private set
    private lateinit var discovery: Discovery
    private var pairingServer: PairingServer? = null
    private var link: AgentLink? = null
    private var mirror: MirrorCapturer? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var calls: CallWatcher? = null
    // Clipboard sync, text only: what a computer copied goes on the phone's
    // clipboard; what's copied here goes to the computers. Android only lets
    // an app read the clipboard while it's on screen, so copies made in other
    // apps are sent when Curslink can see them (it's in front).
    private lateinit var clipboard: ClipboardManager
    @Volatile private var lastClip: String? = null
    private val clipListener = ClipboardManager.OnPrimaryClipChangedListener { clipChanged() }
    private val main = Handler(Looper.getMainLooper())
    private val netCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) { link?.reconnect() }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        prefs = Prefs(this)
        AppLog.i(TAG, "starting as ${prefs.name} (${prefs.deviceId}); " +
            (prefs.pairing?.let { "paired with ${it.hostName} (${it.hostId}) at ${it.addresses.joinToString().ifEmpty { "no address yet" }}" +
                (if (it.hasRelay) ", relay ${it.relay}" else "") } ?: "not paired"))
        discovery = Discovery(this)
        goForeground()
        pairingServer = PairingServer(
            me = { DeviceInfo(prefs.deviceId, prefs.name, "agent") },
            about = ::about,
            askCode = ::askCode,
            onPaired = ::paired,
            onFailed = { reason -> AppLog.i(TAG, "pairing failed: $reason"); clearPairRequest() },
        ).also { it.start() }
        discovery.start(prefs.deviceId, prefs.name, pairingServer!!.boundPort)
        mirror = MirrorCapturer(this, { link?.session?.mirror }, ::mediaForeground)
        clipboard = getSystemService(CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.addPrimaryClipChangedListener(clipListener)
        link = AgentLink(
            handler = Input(),
            mirror = Mirrors(),
            clipboard = ::copiedElsewhere,
            files = Drops.session(this),
            pairing = { prefs.pairing },
            hello = { Hello(prefs.name, platform(), screens(), screenMm()) },
            discover = ::candidates,
            onState = { s, detail ->
                state = s to (if (s == LinkState.PAUSED) pauseWhy() else detail)
                InputService.instance?.holdScreen(prefs.keepScreenOn && s == LinkState.CONNECTED)
                if (s == LinkState.CONNECTED) link?.via?.substringBefore(":")?.takeIf { !it.startsWith("relay") }?.let(prefs::rememberAddress)
                updateNotification()
                PauseTile.refresh(this)
                changed()
            },
            log = { AppLog.i(TAG, it) },
        )
        // Taking a call: get the computer's pointer (and any mirror) off the
        // phone until it's over, unless that's turned off.
        calls = CallWatcher(this) { on ->
            AppLog.i(TAG, if (on) "a call started" else "the call ended")
            applyPause()
        }.also { it.start() }
        applyPause()
        link?.start()
        (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).registerDefaultNetworkCallback(netCallback)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        AppLog.i(TAG, "stopping")
        instance = null
        runCatching { (getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager).unregisterNetworkCallback(netCallback) }
        runCatching { clipboard.removePrimaryClipChangedListener(clipListener) }
        calls?.stop()
        link?.stop()
        mirror?.close()
        pairingServer?.close()
        discovery.stop()
        releaseWifi()
        super.onDestroy()
    }

    /** Paused by hand, or for a call (when that's on). */
    val pausedForCall: Boolean
        get() = !prefs.paused && prefs.pauseDuringCalls && calls?.inCall == true

    private fun pauseWhy() = if (prefs.paused) "paused" else if (pausedForCall) "call" else ""

    /** Pause or resume the link to match the settings and any call. */
    fun applyPause() {
        val want = prefs.paused || pausedForCall
        link?.setPaused(want, if (prefs.paused) "paused here" else "a call is on")
        if (want && state.first == LinkState.PAUSED) state = LinkState.PAUSED to pauseWhy()
        updateNotification()
        changed()
    }

    /** Pairing or settings changed: connect again with them. */
    fun relink() {
        link?.reconnect()
        changed()
    }

    fun unpair() {
        AppLog.i(TAG, "unpaired from ${prefs.pairing?.hostName}")
        prefs.pairing = null
        prefs.layoutSize = null
        relink()
    }

    /** What pairing tells the computer about us, so it can lay us out right away. */
    /** Text a computer copied: on the phone's clipboard, ready to paste. */
    private fun copiedElsewhere(text: String) {
        main.post {
            lastClip = text
            runCatching { clipboard.setPrimaryClip(ClipData.newPlainText("Curslink", text)) }
                .onFailure { AppLog.i(TAG, "couldn't set the clipboard: ${it.message}") }
        }
    }

    /** Called on the main thread when the phone's clipboard changes. Also
     *  worth calling when the app comes to the front (the only time Android
     *  lets it read what other apps copied). */
    fun clipChanged() {
        val text = runCatching { clipboard.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this) }
            .getOrNull()?.toString() ?: return
        if (text.isEmpty() || text == lastClip) return
        lastClip = text
        val session = link?.session?.clipboard ?: return
        Thread({ runCatching { session.copied(text) } }, "curslink-clip").start()
    }

    fun about() = jsonOf("screens" to screens(), "platform" to platform())

    private fun platform() = "Android ${Build.VERSION.RELEASE}"

    /** The one screen we report: the size the computer laid us out with, so
     *  turning the phone doesn't move the edge (InputService maps it). */
    private fun screens(): List<Map<String, Any>> {
        val (w, h) = currentLayout()
        return listOf(mapOf("x" to 0, "y" to 0, "width" to w, "height" to h, "name" to "phone"))
    }

    /** How big the screen really is, in mm (its pixels over the panel's dpi), for mirror mode. */
    private fun screenMm(): List<Map<String, Any>> {
        val (w, h) = currentLayout()
        val px = Screen.sizePx(this)
        val dm = resources.displayMetrics
        val dpi = (dm.xdpi + dm.ydpi) / 2
        if (dpi <= 0f) return emptyList()
        val a = px.x / dpi * 25.4
        val b = px.y / dpi * 25.4
        // Upright, like the layout's size.
        val (mw, mh) = if ((w >= h) == (a >= b)) a to b else b to a
        return listOf(mapOf("x" to 0, "y" to 0, "width" to w, "height" to h,
            "width_mm" to Math.round(mw * 10) / 10.0, "height_mm" to Math.round(mh * 10) / 10.0))
    }

    private fun currentLayout(): Pair<Int, Int> = prefs.layoutSize ?: Screen.naturalSizeDp(this)

    private fun candidates(p: HostPairing): List<Pair<String, Int>> =
        discovery.hosts[p.hostId]?.let { listOf(it.address to it.controlPort) } ?: emptyList()

    // ------------------------------------------------------------ pairing
    private fun askCode(peer: DeviceInfo): String? {
        val req = PairRequest(peer, CompletableFuture())
        pairRequest = req
        changed()
        showPairNotification(peer)
        main.post {
            // An accessibility service lets us open the window from the background.
            runCatching {
                startActivity(Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP))
            }
        }
        return try {
            req.answer.get(120, TimeUnit.SECONDS)
        } catch (e: Exception) {
            null
        } finally {
            clearPairRequest()
        }
    }

    private fun clearPairRequest() {
        pairRequest = null
        getSystemService(NotificationManager::class.java).cancel(NOTE_PAIR)
        changed()
    }

    /** Pairing finished (from a computer's request, or a scanned QR code). */
    fun paired(result: PairResult, address: String, controlPort: Int? = null, relay: Pair<String, String>? = null) {
        val old = prefs.pairing
        val (relayAddress, relayKey) = relay?.takeIf { it.first.isNotEmpty() } ?: prefs.relay
        if (prefs.layoutSize == null || old?.hostId != result.peer.deviceId) prefs.layoutSize = currentLayout()
        prefs.pairing = HostPairing(
            hostId = result.peer.deviceId,
            hostName = result.peer.name,
            myId = prefs.deviceId,
            key = result.key,
            addresses = listOf(address).filter { it.isNotEmpty() } + (if (old?.hostId == result.peer.deviceId) old.addresses else emptyList()),
            controlPort = controlPort ?: discovery.hosts[result.peer.deviceId]?.controlPort ?: old?.controlPort ?: io.github.orimovo.curslink.core.DEFAULT_PORT,
            relay = relayAddress,
            relayKey = relayKey,
        )
        AppLog.i(TAG, "paired with ${result.peer.name} (${result.peer.deviceId}) at ${address.ifEmpty { "?" }}" +
            (if (relayAddress.isNotEmpty()) ", relay $relayAddress" else ""))
        relink()
    }

    // ------------------------------------------------------------- input
    private inner class Input : InputHandler {
        override fun enter(x: Double, y: Double, effect: String) {
            AppLog.i(TAG, "control came here at ($x, $y)" + if (InputService.instance == null) "; the accessibility service is off, so nothing happens" else "")
            holdWifi()
            Waker.wake(this@LinkService)
            InputService.instance?.enter(x, y, effect)
        }

        override fun leave(effect: String, x: Double?, y: Double?) {
            AppLog.i(TAG, "control went back")
            releaseWifi()
            InputService.instance?.leave(effect)
        }

        override fun move(x: Double, y: Double) { InputService.instance?.move(x, y) }
        override fun button(name: String, pressed: Boolean) { InputService.instance?.button(name, pressed) }
        override fun scroll(dx: Double, dy: Double) { InputService.instance?.scroll(dx, dy) }
        override fun key(key: String, pressed: Boolean) { InputService.instance?.key(key, pressed) }
        override fun edge(cmd: io.github.orimovo.curslink.core.EdgeCommand) { InputService.instance?.edge(cmd) }
    }

    // ------------------------------------------------------------- mirrors
    /** Mirror mode (core/Mirror.kt): show what's behind the phone, share what's behind something else. */
    private inner class Mirrors : MirrorHandler {
        override fun state(captures: List<MirrorCapture>, shows: List<MirrorShow>) {
            if (captures.isNotEmpty() || shows.isNotEmpty()) {
                AppLog.i(TAG, "mirror mode: capturing ${captures.size}, showing ${shows.size}")
            }
            InputService.instance?.mirrorShows(shows) { id, data -> link?.session?.mirror?.tune(id, data) }
            mirror?.set(captures)
        }

        override fun image(id: String, jpeg: ByteArray) {
            val bmp = BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size) ?: return
            InputService.instance?.mirrorImage(id, bmp) ?: bmp.recycle()
        }

        override fun pointer(id: String, x: Double?, y: Double?) {
            InputService.instance?.mirrorPointer(id, x, y)
        }
    }

    /** Android wants a media projection foreground service while the screen is shared. */
    // Called on the capturer's thread, and done before it returns: on Android
    // 14 the service type has to be there before the projection is opened.
    private fun mediaForeground(on: Boolean) {
        if (Build.VERSION.SDK_INT < 29) return
        val type = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE or
            (if (on) ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION else 0)
        runCatching { startForeground(NOTE_LINK, buildNotification(), type) }
            .onFailure { AppLog.w(TAG, "couldn't change the foreground service for screen sharing", it) }
    }

    /** While control is here: keep Wi-Fi out of power saving, for smooth moves. */
    private fun holdWifi() {
        if (wifiLock?.isHeld == true) return
        val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
        @Suppress("DEPRECATION")
        val mode = if (Build.VERSION.SDK_INT >= 29) WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
        wifiLock = wm.createWifiLock(mode, "curslink:active").apply { setReferenceCounted(false); acquire() }
    }

    private fun releaseWifi() {
        wifiLock?.let { runCatching { it.release() } }
        wifiLock = null
    }

    // ------------------------------------------------------- notifications
    private fun goForeground() {
        val note = buildNotification()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTE_LINK, note, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        } else {
            startForeground(NOTE_LINK, note)
        }
    }

    private fun buildNotification(): Notification {
        val p = prefs.pairing
        val (s, detail) = state
        val text = when {
            p == null -> "Not paired yet. Open Curslink to pair with your computer."
            s == LinkState.PAUSED && detail == "call" -> "Paused for your call. Back on ${p.hostName} when it ends."
            s == LinkState.PAUSED -> "Paused. ${p.hostName}'s mouse and keyboard won't come over until you resume."
            s == LinkState.CONNECTED -> "Linked to ${p.hostName}" + if (detail.startsWith("relay")) " through your relay" else ""
            else -> "Looking for ${p.hostName}…"
        }
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val b = NotificationCompat.Builder(this, CHANNEL_LINK)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setOngoing(true)
            .setSilent(true)
            .setContentIntent(open)
        if (p != null) {
            val resume = prefs.paused
            val action = Intent(this, PauseReceiver::class.java)
                .setAction(if (resume) PauseReceiver.ACTION_RESUME else PauseReceiver.ACTION_PAUSE)
            val pi = PendingIntent.getBroadcast(this, 2, action, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            b.addAction(0, if (resume) "Resume" else "Pause", pi)
        }
        return b.build()
    }

    private fun updateNotification() {
        runCatching { getSystemService(NotificationManager::class.java).notify(NOTE_LINK, buildNotification()) }
    }

    private fun showPairNotification(peer: DeviceInfo) {
        val open = PendingIntent.getActivity(this, 1, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val note = NotificationCompat.Builder(this, CHANNEL_PAIR)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("${peer.name} wants to pair")
            .setContentText("Tap to type the code it shows.")
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setContentIntent(open)
            .build()
        runCatching { getSystemService(NotificationManager::class.java).notify(NOTE_PAIR, note) }
    }
}
