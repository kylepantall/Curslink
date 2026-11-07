package io.github.orimovo.curslink

import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.os.PowerManager
import android.util.Log

/** The mouse arrived: turn the screen on and bring up the unlock screen. */
object Waker {
    fun wake(context: Context) {
        val pm = context.getSystemService(Context.POWER_SERVICE) as PowerManager
        val km = context.getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager
        if (pm.isInteractive && !km.isKeyguardLocked) return
        // The one documented way for an app to light the screen on its own.
        @Suppress("DEPRECATION")
        val lock = pm.newWakeLock(
            PowerManager.SCREEN_BRIGHT_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP or PowerManager.ON_AFTER_RELEASE,
            "curslink:wake",
        )
        lock.acquire(3000)
        // Shows over the lock screen and asks to dismiss it: straight in without
        // a secure lock (or with Smart Lock), else the PIN / face / fingerprint
        // screen. Starting it from the background is allowed because the
        // accessibility service is running.
        val ctx = InputService.instance ?: context
        runCatching {
            ctx.startActivity(
                Intent(ctx, WakeActivity::class.java).addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_NO_ANIMATION or Intent.FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS,
                ),
            )
        }.onFailure { Log.w("CurslinkWake", "couldn't show over the lock screen: $it") }
    }
}
