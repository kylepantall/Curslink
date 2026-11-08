package io.github.orimovo.curslink

import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log
import androidx.core.content.FileProvider
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * What the app did, kept in a small file next to logcat, so "Export logs"
 * can hand it over (logcat itself isn't readable by other apps, or by us
 * across a restart). Two files of up to [MAX_BYTES], the older one replaced.
 */
object AppLog {
    private const val MAX_BYTES = 512 * 1024L
    private var dir: File? = null
    private val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.US)

    fun init(context: Context) {
        dir = File(context.filesDir, "logs").apply { mkdirs() }
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, e ->
            runCatching { e("Crash", "uncaught on ${thread.name}", e) }
            previous?.uncaughtException(thread, e)
        }
    }

    fun i(tag: String, message: String) {
        Log.i(tag, message)
        write("I", tag, message)
    }

    fun w(tag: String, message: String, e: Throwable? = null) {
        Log.w(tag, message, e)
        write("W", tag, message + (e?.let { "\n" + trace(it) } ?: ""))
    }

    fun e(tag: String, message: String, e: Throwable? = null) {
        Log.e(tag, message, e)
        write("E", tag, message + (e?.let { "\n" + trace(it) } ?: ""))
    }

    private fun trace(e: Throwable) = StringWriter().also { e.printStackTrace(PrintWriter(it)) }.toString().trimEnd()

    @Synchronized private fun write(level: String, tag: String, message: String) {
        val d = dir ?: return
        runCatching {
            val file = File(d, "curslink.log")
            if (file.length() > MAX_BYTES) file.renameTo(File(d, "curslink.1.log"))
            file.appendText("${stamp.format(Date())} $level $tag: $message\n")
        }
    }

    /** Both files, oldest first, after a header saying what this phone is. */
    @Synchronized fun bundle(context: Context, extra: String): File {
        val out = File(context.cacheDir, "export").apply { mkdirs() }
        val file = File(out, "curslink-android-${SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())}.txt")
        val version = runCatching {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
        }.getOrNull() ?: "?"
        out.listFiles()?.filter { it != file }?.forEach { it.delete() }
        file.bufferedWriter().use { w ->
            w.write("Curslink for Android $version\n")
            w.write("Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT}) on ${Build.MANUFACTURER} ${Build.MODEL}\n")
            w.write("Exported ${stamp.format(Date())}\n")
            w.write(extra.trimEnd() + "\n\n")
            for (name in listOf("curslink.1.log", "curslink.log")) {
                val f = dir?.let { File(it, name) } ?: continue
                if (f.exists()) f.bufferedReader().use { it.copyTo(w) }
            }
        }
        return file
    }

    /** Opens the share sheet with the log file (to email, save to Drive, send to a computer…). */
    fun share(context: Context, extra: String) {
        val file = bundle(context, extra)
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.logs", file)
        val send = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_STREAM, uri)
            putExtra(Intent.EXTRA_SUBJECT, "Curslink Android logs")
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        context.startActivity(Intent.createChooser(send, "Export Curslink logs").addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }
}
