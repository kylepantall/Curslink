pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
    plugins {
        id("com.android.application") version "8.7.3"
        id("org.jetbrains.kotlin.android") version "2.0.21"
        id("org.jetbrains.kotlin.jvm") version "2.0.21"
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "curslink-android"

// The protocol, pairing and TLS live in :core, a plain JVM library, so they
// can be built and tested (against the Python host, see interop/) without
// the Android SDK. CURSLINK_CORE_ONLY=1 leaves the app out for that.
include(":core")
if (System.getenv("CURSLINK_CORE_ONLY").isNullOrEmpty()) {
    include(":app")
}
