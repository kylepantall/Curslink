package io.github.orimovo.curslink.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import java.io.File
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest

/**
 * Finding and fetching a newer app from the project's GitHub releases, the
 * same releases (and SHA256SUMS.txt) the desktop apps update from. Every
 * release carries `curslink-<version>-android.apk`. The repository is
 * public, so no token is needed. Installing is the app's part.
 */
object Updates {
    const val REPO = "kylepantall/Curslink"
    const val LATEST_API = "https://api.github.com/repos/$REPO/releases/latest"
    const val SUMS_NAME = "SHA256SUMS.txt"
    private const val APK_SUFFIX = "-android.apk"
    private val json = Json { ignoreUnknownKeys = true }

    class UpdateError(message: String) : IOException(message)

    /** A release with an Android app in it. */
    data class Release(
        val tag: String,
        val version: String,
        val apkName: String,
        val apkUrl: String,
        val apkSize: Long,
        val sumsUrl: String?,
        val page: String,
        val notes: String,
    )

    // ------------------------------------------------------------ versions
    private val VERSION = Regex("""^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$""")

    /** Orders `1.2.3` / `v1.2.3-rc.1` like the desktop updater: a release after its pre-releases. */
    fun compareVersions(a: String, b: String): Int? {
        val x = VERSION.matchEntire(a.trim()) ?: return null
        val y = VERSION.matchEntire(b.trim()) ?: return null
        for (i in 1..3) {
            val c = x.groupValues[i].toLong().compareTo(y.groupValues[i].toLong())
            if (c != 0) return c
        }
        val pa = x.groups[4]?.value
        val pb = y.groups[4]?.value
        if (pa == null || pb == null) return if (pa == pb) 0 else if (pa == null) 1 else -1
        val sa = pa.split(".")
        val sb = pb.split(".")
        for (i in 0 until minOf(sa.size, sb.size)) {
            val na = sa[i].toLongOrNull()
            val nb = sb[i].toLongOrNull()
            val c = when {
                na != null && nb != null -> na.compareTo(nb)
                na != null -> -1  // numbers sort before words
                nb != null -> 1
                else -> sa[i].compareTo(sb[i])
            }
            if (c != 0) return c
        }
        return sa.size.compareTo(sb.size)
    }

    fun isNewer(candidate: String, current: String): Boolean = (compareVersions(candidate, current) ?: 0) > 0

    // ------------------------------------------------------------- parsing
    /** The app in GitHub's release JSON, or null for a draft, a pre-release or one without an APK. */
    fun parseRelease(text: String): Release? {
        val r = json.parseToJsonElement(text).jsonObject
        fun str(o: JsonObject, k: String) = o[k]?.takeIf { it is kotlinx.serialization.json.JsonPrimitive }?.jsonPrimitive?.content
        if (r["draft"]?.jsonPrimitive?.booleanOrNull == true || r["prerelease"]?.jsonPrimitive?.booleanOrNull == true) return null
        val tag = str(r, "tag_name") ?: return null
        val version = tag.removePrefix("v")
        if (compareVersions(version, "0.0.0") == null) return null
        val assets = r["assets"]?.jsonArray?.map { it.jsonObject } ?: emptyList()
        val apk = assets.firstOrNull { str(it, "name")?.endsWith(APK_SUFFIX) == true } ?: return null
        val sums = assets.firstOrNull { str(it, "name") == SUMS_NAME }
        return Release(
            tag = tag,
            version = version,
            apkName = str(apk, "name")!!,
            apkUrl = str(apk, "browser_download_url") ?: return null,
            apkSize = apk["size"]?.jsonPrimitive?.longOrNull ?: -1,
            sumsUrl = sums?.let { str(it, "browser_download_url") },
            page = str(r, "html_url") ?: "https://github.com/$REPO/releases/tag/$tag",
            notes = str(r, "body") ?: "",
        )
    }

    /** `sha256  name` lines (as `sha256sum` writes them) → name to lowercase hash. */
    fun parseSums(text: String): Map<String, String> = text.lines().mapNotNull { line ->
        val parts = line.trim().split(Regex("\\s+"), limit = 2)
        if (parts.size == 2 && parts[0].matches(Regex("[0-9a-fA-F]{64}"))) parts[1].removePrefix("*") to parts[0].lowercase() else null
    }.toMap()

    fun sha256(file: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buf = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    // ------------------------------------------------------------- network
    private fun open(url: String, timeoutMs: Int): HttpURLConnection =
        (URL(url).openConnection() as HttpURLConnection).apply {
            connectTimeout = timeoutMs
            readTimeout = timeoutMs
            instanceFollowRedirects = true  // assets redirect to GitHub's file host
            setRequestProperty("User-Agent", "Curslink-Android")
            setRequestProperty("Accept", if (url.startsWith("https://api.github.com/")) "application/vnd.github+json" else "*/*")
        }

    private fun getText(url: String, timeoutMs: Int): String {
        val c = open(url, timeoutMs)
        try {
            if (c.responseCode != 200) throw UpdateError("GitHub answered ${c.responseCode} for $url")
            return c.inputStream.use { it.readBytes().decodeToString() }
        } finally {
            c.disconnect()
        }
    }

    /** The newest release with an Android app, or null if there isn't one. */
    fun latest(timeoutMs: Int = 15_000, get: (String) -> String = { getText(it, timeoutMs) }): Release? =
        parseRelease(get(LATEST_API))

    /**
     * Download the release's APK into [dir] and check it against
     * SHA256SUMS.txt; a file there from an earlier try is kept if it matches.
     * Throws [UpdateError] for anything that can't be verified.
     */
    fun download(
        release: Release,
        dir: File,
        timeoutMs: Int = 30_000,
        getText: (String) -> String = { this.getText(it, timeoutMs) },
        progress: (Long, Long) -> Unit = { _, _ -> },
        fetch: (String, File, (Long, Long) -> Unit) -> Unit = { url, to, p -> fetchFile(url, to, timeoutMs, p) },
    ): File {
        val sumsUrl = release.sumsUrl ?: throw UpdateError("${release.tag} has no $SUMS_NAME; not installing an unverified app")
        val want = parseSums(getText(sumsUrl))[release.apkName]
            ?: throw UpdateError("${release.apkName} isn't listed in $SUMS_NAME; not installing it")
        dir.mkdirs()
        val target = File(dir, release.apkName)
        if (target.isFile && sha256(target) == want) return target
        // Anything else in there is an older download.
        dir.listFiles()?.forEach { it.delete() }
        val part = File(dir, release.apkName + ".part")
        fetch(release.apkUrl, part, progress)
        if (sha256(part) != want) {
            part.delete()
            throw UpdateError("${release.apkName} doesn't match $SUMS_NAME (corrupt or tampered download); not installing it")
        }
        if (!part.renameTo(target)) throw UpdateError("couldn't save ${release.apkName}")
        return target
    }

    private fun fetchFile(url: String, to: File, timeoutMs: Int, progress: (Long, Long) -> Unit) {
        val c = open(url, timeoutMs)
        try {
            if (c.responseCode != 200) throw UpdateError("GitHub answered ${c.responseCode} for the download")
            val total = c.contentLengthLong
            var done = 0L
            c.inputStream.use { input ->
                to.outputStream().use { out ->
                    val buf = ByteArray(64 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n < 0) break
                        out.write(buf, 0, n)
                        done += n
                        progress(done, total)
                    }
                }
            }
        } finally {
            c.disconnect()
        }
    }
}
