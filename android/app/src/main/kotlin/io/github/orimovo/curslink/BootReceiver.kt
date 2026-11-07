package io.github.orimovo.curslink

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** Stay linked after a restart or an update, once paired. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (Prefs(context).pairing != null) runCatching { LinkService.start(context) }
    }
}
