package io.github.orimovo.curslink

import android.app.Application
import android.app.NotificationChannel
import android.app.NotificationManager

class CurslinkApp : Application() {
    override fun onCreate() {
        super.onCreate()
        AppLog.init(this)
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(
            NotificationChannel(LinkService.CHANNEL_LINK, "Staying linked", NotificationManager.IMPORTANCE_MIN).apply {
                description = "Shown while Curslink keeps the phone ready for your computer's mouse and keyboard."
            },
        )
        nm.createNotificationChannel(
            NotificationChannel(LinkService.CHANNEL_PAIR, "Pairing requests", NotificationManager.IMPORTANCE_HIGH),
        )
        nm.createNotificationChannel(
            NotificationChannel(LinkService.CHANNEL_FILES, "Dropped files", NotificationManager.IMPORTANCE_DEFAULT).apply {
                description = "Files dragged here from a computer and let go of on the phone: tap to see them in Downloads."
            },
        )
    }
}
