package io.github.orimovo.curslink

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager

class CurslinkApp : Application() {
    override fun onCreate() {
        super.onCreate()
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(LinkService.CHANNEL_LINK, "Staying linked", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Shown while Curslink keeps the phone ready for your computer's mouse and keyboard."
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(LinkService.CHANNEL_PAIR, "Pairing requests", NotificationManager.IMPORTANCE_HIGH),
        )
    }
}
