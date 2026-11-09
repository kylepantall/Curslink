package io.github.orimovo.curslink.core

import java.io.File
import java.nio.file.Files
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class UpdatesTest {
    private fun releaseJson(tag: String = "v0.4.57", prerelease: Boolean = false, apk: Boolean = true) = """
        {"tag_name": "$tag", "draft": false, "prerelease": $prerelease, "html_url": "https://github.com/x/releases/tag/$tag",
         "body": "What's new", "assets": [
           {"name": "curslink-${tag.removePrefix("v")}-windows-x64-setup.exe", "size": 9, "browser_download_url": "https://example/exe"},
           ${if (apk) """{"name": "curslink-${tag.removePrefix("v")}-android.apk", "size": 6548625, "browser_download_url": "https://example/apk"},""" else ""}
           {"name": "SHA256SUMS.txt", "size": 690, "browser_download_url": "https://example/sums"}
        ]}
    """.trimIndent()

    @Test
    fun versionsOrderLikeTheDesktopUpdater() {
        assertTrue(Updates.isNewer("0.4.57", "0.4.55"))
        assertTrue(Updates.isNewer("v0.10.0", "0.9.9"))
        assertTrue(Updates.isNewer("0.4.57", "0.4.57-dev"))  // a CI build gives way to the release
        assertTrue(Updates.isNewer("1.0.0-rc.2", "1.0.0-rc.1"))
        assertTrue(Updates.isNewer("1.0.0-rc.10", "1.0.0-rc.9"))
        assertFalse(Updates.isNewer("0.4.55", "0.4.55"))
        assertFalse(Updates.isNewer("0.4.54", "0.4.55"))
        assertFalse(Updates.isNewer("nonsense", "0.4.55"))
    }

    @Test
    fun theReleaseJsonGivesTheApkAndItsSums() {
        val r = Updates.parseRelease(releaseJson())!!
        assertEquals("0.4.57", r.version)
        assertEquals("curslink-0.4.57-android.apk", r.apkName)
        assertEquals("https://example/apk", r.apkUrl)
        assertEquals(6548625L, r.apkSize)
        assertEquals("https://example/sums", r.sumsUrl)
        assertNull(Updates.parseRelease(releaseJson(apk = false)))
        assertNull(Updates.parseRelease(releaseJson(prerelease = true)))
        assertEquals("v0.4.57", Updates.latest { releaseJson() }!!.tag)
    }

    @Test
    fun sumsParseLikeSha256sumWritesThem() {
        val sums = Updates.parseSums("""
            fd3e0b8d78b5c8bae1259ef682196b4462c08067cea106ec8a76b6f6db936545  curslink-0.4.55-android.apk
            4E00D1BEC9C94616C1F18CEBE4BD2DA6E6EBA49E5174A6E2275CBEB50970B3DB *curslink-0.4.55-linux-x64.deb
            not a line
        """.trimIndent())
        assertEquals(2, sums.size)
        assertEquals("fd3e0b8d78b5c8bae1259ef682196b4462c08067cea106ec8a76b6f6db936545", sums["curslink-0.4.55-android.apk"])
        assertEquals("4e00d1bec9c94616c1f18cebe4bd2da6e6eba49e5174a6e2275cbeb50970b3db", sums["curslink-0.4.55-linux-x64.deb"])
    }

    @Test
    fun onlyADownloadMatchingTheSumsIsKept() {
        val dir = Files.createTempDirectory("updates").toFile()
        try {
            val r = Updates.parseRelease(releaseJson())!!
            val apk = "pretend apk".toByteArray()
            val good = File(dir, "x").apply { writeBytes(apk) }.let(Updates::sha256).also { File(dir, "x").delete() }
            var fetches = 0
            val fetch: (String, File, (Long, Long) -> Unit) -> Unit = { url, to, _ -> fetches++; assertEquals(r.apkUrl, url); to.writeBytes(apk) }

            File(dir, "curslink-0.4.56-android.apk").writeText("an older download")
            val file = Updates.download(r, dir, getText = { "$good  ${r.apkName}\n" }, fetch = fetch)
            assertEquals(r.apkName, file.name)
            assertEquals(apk.toList(), file.readBytes().toList())
            assertEquals(listOf(r.apkName), dir.list()!!.toList())

            // Already here and still matching: not fetched again.
            Updates.download(r, dir, getText = { "$good  ${r.apkName}\n" }, fetch = fetch)
            assertEquals(1, fetches)

            // Tampered with, or not listed: refused, and nothing is left to install.
            val bad = "0".repeat(64)
            assertFailsWith<Updates.UpdateError> { Updates.download(r, dir, getText = { "$bad  ${r.apkName}\n" }, fetch = fetch) }
            assertTrue(dir.list()!!.isEmpty())
            assertFailsWith<Updates.UpdateError> { Updates.download(r, dir, getText = { "$good  other.apk\n" }, fetch = fetch) }
            assertFailsWith<Updates.UpdateError> { Updates.download(r.copy(sumsUrl = null), dir, fetch = fetch) }
        } finally {
            dir.deleteRecursively()
        }
    }
}
