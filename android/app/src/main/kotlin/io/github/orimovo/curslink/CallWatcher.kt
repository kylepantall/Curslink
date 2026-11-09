package io.github.orimovo.curslink

import android.content.Context
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper

/**
 * Tells whether a call is on: ringing, a phone call, or a call in an app
 * (WhatsApp, Teams, Meet and the like put the phone in "communication" mode).
 * Watches the audio mode, so it needs no phone permission.
 */
class CallWatcher(context: Context, private val changed: (Boolean) -> Unit) {
    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val main = Handler(Looper.getMainLooper())
    @Volatile var inCall = false
        private set
    private var listener: Any? = null
    private val poll = object : Runnable {
        override fun run() {
            check()
            main.postDelayed(this, 2000)
        }
    }

    fun start() {
        inCall = calling(audio.mode)
        if (Build.VERSION.SDK_INT >= 31) {
            val l = AudioManager.OnModeChangedListener { main.post { check() } }
            runCatching { audio.addOnModeChangedListener(mainExecutor(), l) }
                .onSuccess { listener = l }
                .onFailure { main.postDelayed(poll, 2000) }
        } else {
            main.postDelayed(poll, 2000)  // no listener before Android 12
        }
    }

    fun stop() {
        main.removeCallbacks(poll)
        if (Build.VERSION.SDK_INT >= 31) {
            (listener as? AudioManager.OnModeChangedListener)?.let { runCatching { audio.removeOnModeChangedListener(it) } }
        }
        listener = null
    }

    private fun mainExecutor() = java.util.concurrent.Executor { main.post(it) }

    private fun check() {
        val now = calling(audio.mode)
        if (now != inCall) {
            inCall = now
            changed(now)
        }
    }

    private fun calling(mode: Int) =
        mode == AudioManager.MODE_RINGTONE || mode == AudioManager.MODE_IN_CALL ||
            mode == AudioManager.MODE_IN_COMMUNICATION ||
            (Build.VERSION.SDK_INT >= 30 && mode == AudioManager.MODE_CALL_SCREENING) ||
            (Build.VERSION.SDK_INT >= 33 && mode == AudioManager.MODE_CALL_REDIRECT) ||
            (Build.VERSION.SDK_INT >= 33 && mode == AudioManager.MODE_COMMUNICATION_REDIRECT)
}
