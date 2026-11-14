package io.github.orimovo.curslink

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import java.io.ByteArrayOutputStream
import java.io.File
import kotlin.concurrent.thread

/**
 * Auto line-up (see curslink/lineup.py on the computer): every screen shows
 * its patterns, and this takes the one photo of the desk the computer reads
 * them from. The phone's own camera app takes it (full resolution, its own
 * focus and exposure); we shrink it and send it over the link.
 */
class LineupActivity : AppCompatActivity() {
    private var id = ""
    private var file: File? = null
    private var sent = false

    private val askCamera = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) shoot() else giveUp("Curslink needs the camera to take the photo.")
    }

    private val takePicture = registerForActivityResult(ActivityResultContracts.TakePicture()) { taken ->
        val f = file
        if (!taken || f == null || !f.exists() || f.length() == 0L) {
            giveUp(null)
            return@registerForActivityResult
        }
        Toast.makeText(this, "Sending the photo…", Toast.LENGTH_SHORT).show()
        val lineupId = id
        thread(name = "curslink-lineup") {
            val ok = runCatching {
                val jpeg = shrink(f)
                LinkService.instance?.lineupPhoto(lineupId, jpeg) ?: error("not linked")
            }
            f.delete()
            runOnUiThread {
                ok.onFailure { AppLog.w(TAG, "couldn't send the line-up photo", it) }
                Toast.makeText(this,
                    if (ok.isSuccess) "Sent. Check the layout on your computer." else "Couldn't send the photo: ${ok.exceptionOrNull()?.message}",
                    Toast.LENGTH_LONG).show()
                sent = true
                finish()
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        id = intent.getStringExtra(EXTRA_ID) ?: run { finish(); return }
        current = this
        if (savedInstanceState != null) {
            file = savedInstanceState.getString("file")?.let(::File)
            return  // the camera's result is on its way
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) shoot()
        else askCamera.launch(Manifest.permission.CAMERA)
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        file?.let { outState.putString("file", it.path) }
    }

    override fun onDestroy() {
        if (current === this) current = null
        super.onDestroy()
    }

    private fun shoot() {
        val dir = File(cacheDir, "lineup").apply { mkdirs() }
        dir.listFiles()?.forEach { it.delete() }
        val f = File(dir, "desk-$id.jpg")
        file = f
        val uri: Uri = FileProvider.getUriForFile(this, "$packageName.logs", f)
        Toast.makeText(this, "Get every screen in the photo, with the patterns sharp.", Toast.LENGTH_LONG).show()
        runCatching { takePicture.launch(uri) }.onFailure { giveUp("No camera app to take the photo with.") }
    }

    private fun giveUp(why: String?) {
        if (why != null) Toast.makeText(this, why, Toast.LENGTH_LONG).show()
        val lineupId = id
        thread(name = "curslink-lineup") { runCatching { LinkService.instance?.lineupCancel(lineupId) } }
        sent = true
        finish()
    }

    companion object {
        private const val TAG = "CurslinkLineup"
        const val EXTRA_ID = "id"
        /** Up to this many pixels along the photo's longest side (the computer reads it at 3000). */
        private const val LONGEST = 3000
        @Volatile private var current: LineupActivity? = null

        /** The computer wants a photo for line-up [id] (or, [cancel], not any more). */
        fun ask(context: Context, id: String, cancel: Boolean) {
            if (cancel) {
                current?.let { a -> a.runOnUiThread { if (a.id == id && !a.sent) a.finish() } }
                return
            }
            // Starting it from the background is allowed while the accessibility service runs (as WakeActivity).
            val ctx = InputService.instance ?: context
            runCatching {
                ctx.startActivity(Intent(ctx, LineupActivity::class.java)
                    .putExtra(EXTRA_ID, id)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP))
            }.onFailure { AppLog.w(TAG, "couldn't open the camera for the line-up: $it") }
        }

        /** The photo, at most [LONGEST] pixels along its longest side, as a JPEG. */
        fun shrink(f: File): ByteArray {
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(f.path, bounds)
            var sample = 1
            while (maxOf(bounds.outWidth, bounds.outHeight) / (sample * 2) >= LONGEST) sample *= 2
            val bmp = BitmapFactory.decodeFile(f.path, BitmapFactory.Options().apply { inSampleSize = sample })
                ?: error("the camera's photo couldn't be read")
            val longest = maxOf(bmp.width, bmp.height)
            val scaled = if (longest > LONGEST) {
                val k = LONGEST.toDouble() / longest
                Bitmap.createScaledBitmap(bmp, (bmp.width * k).toInt(), (bmp.height * k).toInt(), true).also { bmp.recycle() }
            } else bmp
            val out = ByteArrayOutputStream()
            scaled.compress(Bitmap.CompressFormat.JPEG, 90, out)
            scaled.recycle()
            return out.toByteArray()
        }
    }
}
