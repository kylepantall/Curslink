package io.github.orimovo.curslink

import android.app.Notification
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageInstaller
import android.net.ConnectivityManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import io.github.orimovo.curslink.core.Updates
import java.io.File
import kotlin.concurrent.thread

/**
 * Keeps the app up to date from GitHub releases, like the desktop apps: it
 * looks a few times a day, downloads the new APK (checked against the
 * release's SHA256SUMS.txt) and installs it with Android's package
 * installer. Android asks you to confirm the first update; after that
 * Curslink is the app's installer and updates go in by themselves (Android
 * 12+), which we do while the screen is off so a link in use isn't cut.
 */
object Updater {
    private const val TAG = "CurslinkUpdate"
    const val CHANNEL_UPDATES = "updates"
    private const val NOTE_UPDATE = 3
    private const val HOUR = 60 * 60 * 1000L
    /** How often the service looks by itself. */
    const val CHECK_EVERY_MS = 6 * HOUR

    enum class Phase { IDLE, CHECKING, AVAILABLE, DOWNLOADING, READY, INSTALLING, FAILED }

    @Volatile var phase = Phase.IDLE
        private set
    @Volatile var release: Updates.Release? = null
        private set
    @Volatile var message = ""
        private set
    @Volatile var lastChecked = 0L
        private set
    /** True while the app's window is in front: an install prompt can open at once. */
    @Volatile var inFront = false

    private val main = Handler(Looper.getMainLooper())
    private val listeners = mutableSetOf<() -> Unit>()
    fun listen(l: () -> Unit) = synchronized(listeners) { listeners.add(l) }
    fun unlisten(l: () -> Unit) = synchronized(listeners) { listeners.remove(l) }
    private fun show(p: Phase, msg: String = message) {
        phase = p
        phaseSince = System.currentTimeMillis()
        message = msg
        val ls = synchronized(listeners) { listeners.toList() }
        main.post { ls.forEach { it() } }
    }

    @Volatile private var phaseSince = 0L
    private var screenOffWait: BroadcastReceiver? = null

    fun currentVersion(context: Context): String =
        runCatching { context.packageManager.getPackageInfo(context.packageName, 0).versionName }.getOrNull() ?: "0.0.0"

    /** Mid-check, download or install; an install prompt left unanswered stops counting after a while. */
    private fun busy() = (phase == Phase.CHECKING || phase == Phase.DOWNLOADING || phase == Phase.INSTALLING) &&
        System.currentTimeMillis() - phaseSince < 15 * 60 * 1000L

    /**
     * Look for a newer release if the last look is older than [maxAgeMs]
     * (0: now). [byHand] is "Check for updates" or "Update now": it downloads
     * and installs whatever the settings say, and shows Android's prompt at once.
     */
    fun check(context: Context, maxAgeMs: Long = CHECK_EVERY_MS, byHand: Boolean = false) {
        val app = context.applicationContext
        val prefs = Prefs(app)
        if (!byHand && !prefs.autoUpdate) return
        if (busy()) return
        if (!byHand && System.currentTimeMillis() - prefs.updateCheckedAt < maxAgeMs) {
            lastChecked = prefs.updateCheckedAt
            return
        }
        show(Phase.CHECKING, "Checking for updates…")
        thread(name = "curslink-update", isDaemon = true) {
            try {
                val current = currentVersion(app)
                val latest = Updates.latest()
                lastChecked = System.currentTimeMillis()
                prefs.updateCheckedAt = lastChecked
                if (latest == null || !Updates.isNewer(latest.version, current)) {
                    release = null
                    cancelNote(app)
                    cleanDownloads(app)
                    AppLog.i(TAG, "up to date ($current; newest ${latest?.version ?: "none"})")
                    show(Phase.IDLE, "")
                    return@thread
                }
                release = latest
                AppLog.i(TAG, "${latest.version} is out (this is $current)")
                val metered = app.getSystemService(ConnectivityManager::class.java)?.isActiveNetworkMetered ?: true
                if (!byHand && metered) {
                    // Wait for Wi-Fi to download; say it's there meanwhile.
                    show(Phase.AVAILABLE, "Curslink ${latest.version} is out. It downloads on Wi-Fi, or tap Update now.")
                    notifyOnce(app, "out:${latest.version}", "Curslink ${latest.version} is out", "It'll download on Wi-Fi. Tap to update now.", openApp(app))
                    return@thread
                }
                download(app, latest, byHand)
            } catch (e: Exception) {
                AppLog.w(TAG, "update check failed", e)
                show(Phase.FAILED, "Couldn't check for updates: ${e.message ?: e.javaClass.simpleName}")
            }
        }
    }

    /** "Update now" on an update already found. */
    fun updateNow(context: Context) {
        val app = context.applicationContext
        val r = release ?: return check(app, 0, byHand = true)
        if (busy()) return
        thread(name = "curslink-update", isDaemon = true) {
            try {
                download(app, r, byHand = true)
            } catch (e: Exception) {
                AppLog.w(TAG, "update failed", e)
                show(Phase.FAILED, "Couldn't update: ${e.message ?: e.javaClass.simpleName}")
            }
        }
    }

    private fun downloadDir(context: Context) = File(context.cacheDir, "updates")

    private fun cleanDownloads(context: Context) {
        downloadDir(context).listFiles()?.forEach { it.delete() }
    }

    private fun download(app: Context, r: Updates.Release, byHand: Boolean) {
        show(Phase.DOWNLOADING, "Downloading ${r.version}…")
        var shown = -1
        val apk = Updates.download(r, downloadDir(app), progress = { done, total ->
            val size = if (total > 0) total else r.apkSize
            val pct = if (size > 0) (done * 100 / size).toInt() else -1
            if (pct / 5 != shown / 5) {
                shown = pct
                show(Phase.DOWNLOADING, if (pct >= 0) "Downloading ${r.version}… $pct%" else "Downloading ${r.version}…")
            }
        })
        AppLog.i(TAG, "downloaded and verified ${apk.name}")
        show(Phase.READY, "Curslink ${r.version} is ready to install.")
        if (byHand) install(app, apk, r, byHand) else installWhenIdle(app, apk, r)
    }

    /** Install once the screen is off, so an update doesn't cut a link that's in use. */
    private fun installWhenIdle(app: Context, apk: File, r: Updates.Release) {
        val pm = app.getSystemService(PowerManager::class.java)
        if (!pm.isInteractive) return install(app, apk, r, byHand = false)
        main.post {
            if (screenOffWait != null) return@post
            AppLog.i(TAG, "installing ${r.version} when the screen goes off")
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(context: Context, intent: Intent) {
                    runCatching { app.unregisterReceiver(this) }
                    screenOffWait = null
                    if (phase == Phase.READY && release == r && apk.isFile) install(app, apk, r, byHand = false)
                }
            }
            screenOffWait = receiver
            ContextCompat.registerReceiver(app, receiver, IntentFilter(Intent.ACTION_SCREEN_OFF), ContextCompat.RECEIVER_NOT_EXPORTED)
        }
    }

    private fun install(app: Context, apk: File, r: Updates.Release, byHand: Boolean) {
        show(Phase.INSTALLING, "Installing ${r.version}…")
        try {
            val installer = app.packageManager.packageInstaller
            val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
                setAppPackageName(app.packageName)
                setSize(apk.length())
                if (Build.VERSION.SDK_INT >= 31) setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
                if (Build.VERSION.SDK_INT >= 33) setPackageSource(PackageInstaller.PACKAGE_SOURCE_DOWNLOADED_FILE)
            }
            val id = installer.createSession(params)
            installer.openSession(id).use { session ->
                session.openWrite("base.apk", 0, apk.length()).use { out ->
                    apk.inputStream().use { it.copyTo(out) }
                    session.fsync(out)
                }
                val intent = Intent(app, UpdateReceiver::class.java).putExtra(UpdateReceiver.EXTRA_VERSION, r.version)
                    .putExtra(UpdateReceiver.EXTRA_BY_HAND, byHand)
                // Mutable: the installer adds the result to it.
                val flags = PendingIntent.FLAG_UPDATE_CURRENT or (if (Build.VERSION.SDK_INT >= 31) PendingIntent.FLAG_MUTABLE else 0)
                session.commit(PendingIntent.getBroadcast(app, id, intent, flags).intentSender)
            }
            AppLog.i(TAG, "handed ${apk.name} to the package installer (session $id)")
        } catch (e: Exception) {
            AppLog.w(TAG, "couldn't start the install", e)
            show(Phase.FAILED, "Couldn't install ${r.version}: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    /** What the package installer said (see [UpdateReceiver]). */
    internal fun installResult(app: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val version = intent.getStringExtra(UpdateReceiver.EXTRA_VERSION) ?: release?.version ?: "the update"
        val detail = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm: Intent = (if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java)
                    else intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)) ?: return
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                AppLog.i(TAG, "Android wants you to confirm $version")
                if (inFront && runCatching { app.startActivity(confirm) }.isSuccess) {
                    show(Phase.INSTALLING, "Confirm the update to $version in Android's prompt.")
                    return
                }
                show(Phase.READY, "Curslink $version is ready to install. Tap Update now, or the notification.")
                val tap = PendingIntent.getActivity(app, NOTE_UPDATE, confirm, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
                // Asked by itself: remind once per version, not at every check.
                if (intent.getBooleanExtra(UpdateReceiver.EXTRA_BY_HAND, false)) postNote(app, "Curslink $version is ready", "Tap to install it.", tap)
                else notifyOnce(app, "ready:$version", "Curslink $version is ready", "Tap to install it.", tap)
            }
            PackageInstaller.STATUS_SUCCESS -> {
                // Usually we're stopped before this arrives; the new copy starts from MY_PACKAGE_REPLACED.
                AppLog.i(TAG, "updated to $version")
                cancelNote(app)
                show(Phase.IDLE, "")
            }
            PackageInstaller.STATUS_FAILURE_ABORTED -> {
                AppLog.i(TAG, "update to $version cancelled")
                show(Phase.READY, "Curslink $version is ready to install.")
            }
            else -> {
                AppLog.w(TAG, "update to $version failed: status $status ${detail ?: ""}")
                val why = when (status) {
                    PackageInstaller.STATUS_FAILURE_CONFLICT, PackageInstaller.STATUS_FAILURE_INCOMPATIBLE ->
                        "this copy was signed differently from the releases. Uninstall it and install the APK from the release page once; later updates then come in here."
                    PackageInstaller.STATUS_FAILURE_STORAGE -> "there isn't enough space."
                    PackageInstaller.STATUS_FAILURE_BLOCKED -> "Android blocked it. Allow Curslink to install apps, then try again."
                    else -> detail ?: "status $status."
                }
                show(Phase.FAILED, "Couldn't install $version: $why")
            }
        }
    }

    // ------------------------------------------------------- notifications
    private fun openApp(app: Context): PendingIntent = PendingIntent.getActivity(
        app, NOTE_UPDATE, Intent(app, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
    )

    /** Say a release is out once per version, not at every check. */
    private fun notifyOnce(app: Context, key: String, title: String, text: String, tap: PendingIntent) {
        val prefs = Prefs(app)
        if (key in prefs.updateNotified) return
        prefs.updateNotified = (prefs.updateNotified + key).takeLast(4)
        postNote(app, title, text, tap)
    }

    private fun postNote(app: Context, title: String, text: String, tap: PendingIntent) {
        val note: Notification = NotificationCompat.Builder(app, CHANNEL_UPDATES)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(text)
            .setContentIntent(tap)
            .setAutoCancel(true)
            .build()
        runCatching { app.getSystemService(NotificationManager::class.java).notify(NOTE_UPDATE, note) }
    }

    private fun cancelNote(app: Context) {
        runCatching { app.getSystemService(NotificationManager::class.java).cancel(NOTE_UPDATE) }
    }
}

/** The package installer's answer to an update. */
class UpdateReceiver : BroadcastReceiver() {
    companion object {
        const val EXTRA_VERSION = "io.github.orimovo.curslink.UPDATE_VERSION"
        const val EXTRA_BY_HAND = "io.github.orimovo.curslink.UPDATE_BY_HAND"
    }

    override fun onReceive(context: Context, intent: Intent) = Updater.installResult(context.applicationContext, intent)
}
