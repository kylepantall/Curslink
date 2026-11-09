package io.github.orimovo.curslink

import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import androidx.annotation.RequiresApi
import androidx.core.app.NotificationCompat
import io.github.orimovo.curslink.core.DragCard
import io.github.orimovo.curslink.core.DropHandler
import io.github.orimovo.curslink.core.FileSession
import io.github.orimovo.curslink.core.FileStore
import java.io.File

/**
 * Files dragged to the phone from a computer (core/Files.kt): the card
 * follows the drawn pointer while a drag is over the phone, and files let
 * go of here land in Downloads/Curslink, with a notification to open them.
 */
object Drops {
    private const val TAG = "Drops"
    private const val NOTE_SAVED = 3
    private const val SUBFOLDER = "Curslink"

    fun session(context: Context): FileSession {
        val app = context.applicationContext
        return FileSession(Ui(app), store(app))
    }

    private fun store(context: Context): FileStore {
        val staging = File(context.cacheDir, "drops").apply { mkdirs() }
        if (Build.VERSION.SDK_INT >= 29) return MediaStoreDownloads(context, staging)
        // Before Android 10 a public folder needs a storage permission: keep them in the app's own Downloads.
        val folder = File(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: context.filesDir, SUBFOLDER)
        return object : FileStore(folder, staging) {
            override val where = "Curslink's Downloads folder"
        }
    }

    /** Android 10+: each finished file goes into the shared Downloads, under Curslink. */
    @RequiresApi(29)
    private class MediaStoreDownloads(private val context: Context, staging: File) : FileStore(staging, staging) {
        override val where = "Downloads/$SUBFOLDER"

        override fun publish(dir: File, tops: List<String>): List<String> {
            val resolver = context.contentResolver
            val names = tops.map { top ->
                var shown = top
                File(dir, top).walkTopDown().filter { it.isFile }.forEach { f ->
                    val rel = f.relativeTo(dir).parentFile?.invariantSeparatorsPath
                    val values = ContentValues().apply {
                        put(MediaStore.MediaColumns.DISPLAY_NAME, f.name)
                        put(MediaStore.MediaColumns.RELATIVE_PATH,
                            listOfNotNull(Environment.DIRECTORY_DOWNLOADS, SUBFOLDER, rel).joinToString("/"))
                        MimeTypeMap.getSingleton().getMimeTypeFromExtension(f.extension.lowercase())?.let {
                            put(MediaStore.MediaColumns.MIME_TYPE, it)
                        }
                        put(MediaStore.MediaColumns.IS_PENDING, 1)
                    }
                    val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                        ?: throw java.io.IOException("Downloads wouldn't take ${f.name}")
                    try {
                        resolver.openOutputStream(uri)!!.use { out -> f.inputStream().use { it.copyTo(out) } }
                        resolver.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
                    } catch (e: Exception) {
                        resolver.delete(uri, null, null)
                        throw e
                    }
                    if (rel == null) {
                        // Downloads may have renamed it ("photo (1).jpg") if the name was taken.
                        resolver.query(uri, arrayOf(MediaStore.MediaColumns.DISPLAY_NAME), null, null, null)?.use {
                            if (it.moveToFirst()) shown = it.getString(0) ?: shown
                        }
                    }
                }
                shown
            }
            dir.deleteRecursively()
            return names
        }
    }

    private class Ui(private val context: Context) : DropHandler {
        private val main = Handler(Looper.getMainLooper())
        private var clear: Runnable? = null

        private fun show(title: String?, line: String = "", forMs: Long = 0) = main.post {
            clear?.let { main.removeCallbacks(it) }
            clear = null
            InputService.instance?.dragCard(title, line)
            if (forMs > 0) {
                val r = Runnable { InputService.instance?.dragCard(null) }
                clear = r
                main.postDelayed(r, forMs)
            }
        }

        override fun card(card: DragCard?) {
            if (card == null) show(null) else show(card.title, "${card.kind} · ${size(card.size)}")
        }

        override fun progress(id: String, card: DragCard?, received: Long, total: Long) {
            val pct = if (total > 0) (received * 100 / total).toInt() else 0
            show(card?.title ?: "Receiving files", "Receiving… $pct%")
        }

        override fun saved(id: String, names: List<String>, where: String) {
            AppLog.i(TAG, "saved ${names.joinToString()} in $where")
            show(names.firstOrNull()?.let { if (names.size > 1) "$it and ${names.size - 1} more" else it }, "Saved to $where", 2500)
            notifySaved(names, where)
        }

        override fun failed(id: String, why: String) {
            AppLog.i(TAG, "a drop didn't arrive: $why")
            show("Didn't arrive", why, 3000)
        }

        private fun notifySaved(names: List<String>, where: String) {
            val open = PendingIntent.getActivity(context, 2,
                Intent(android.app.DownloadManager.ACTION_VIEW_DOWNLOADS).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE)
            val title = if (names.size == 1) names[0] else "${names.size} items"
            val note = NotificationCompat.Builder(context, LinkService.CHANNEL_FILES)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText("Dropped here, saved to $where")
                .setAutoCancel(true)
                .setContentIntent(open)
                .build()
            runCatching { context.getSystemService(NotificationManager::class.java).notify(NOTE_SAVED, note) }
        }
    }

    private val DragCard.title get() = if (count > 1) "$name and ${count - 1} more" else name

    /** 1536 -> "1.5 KB", powers of 1000 like the computers' cards (curslink/files/meta.py). */
    fun size(n: Long): String {
        if (n < 1000) return "$n byte${if (n != 1L) "s" else ""}"
        var v = n.toDouble()
        for (unit in listOf("KB", "MB", "GB", "TB")) {
            v /= 1000.0
            if (v < 1000 || unit == "TB") return if (v < 100) "%.1f %s".format(v, unit) else "%.0f %s".format(v, unit)
        }
        return "$n bytes"
    }
}
