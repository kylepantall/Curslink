package io.github.orimovo.curslink

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** The notification's Pause and Resume buttons. */
class PauseReceiver : BroadcastReceiver() {
    companion object {
        const val ACTION_PAUSE = "io.github.orimovo.curslink.PAUSE"
        const val ACTION_RESUME = "io.github.orimovo.curslink.RESUME"
    }

    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            ACTION_PAUSE -> LinkService.setPaused(context, true)
            ACTION_RESUME -> LinkService.setPaused(context, false)
        }
    }
}
