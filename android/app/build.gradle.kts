plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// CI passes the release's version; local builds are 0.0.0.
val curslinkVersion = System.getenv("CURSLINK_VERSION")?.removePrefix("v")?.ifEmpty { null } ?: "0.0.0"
val versionParts = curslinkVersion.split(".").map { it.takeWhile(Char::isDigit).toIntOrNull() ?: 0 } + listOf(0, 0, 0)

android {
    namespace = "io.github.orimovo.curslink"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.orimovo.curslink"
        minSdk = 26
        targetSdk = 35
        versionCode = maxOf(1, versionParts[0] * 10000 + versionParts[1] * 100 + versionParts[2])
        versionName = curslinkVersion
    }

    // Release builds are signed with the key in CURSLINK_ANDROID_KEYSTORE
    // (base64; CI decodes it to a file) so every release can update the last.
    // Without one they fall back to the debug key.
    val keystore = System.getenv("CURSLINK_ANDROID_KEYSTORE_FILE")
    signingConfigs {
        if (!keystore.isNullOrEmpty()) {
            create("release") {
                storeFile = file(keystore)
                storePassword = System.getenv("CURSLINK_ANDROID_KEYSTORE_PASSWORD")
                // Unset secrets arrive as empty strings.
                keyAlias = System.getenv("CURSLINK_ANDROID_KEY_ALIAS")?.ifEmpty { null } ?: "curslink"
                keyPassword = System.getenv("CURSLINK_ANDROID_KEY_PASSWORD")?.ifEmpty { null }
                    ?: System.getenv("CURSLINK_ANDROID_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (!keystore.isNullOrEmpty()) signingConfigs.getByName("release") else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }

    packaging {
        resources {
            // BouncyCastle's jars each carry these.
            excludes += setOf(
                "META-INF/versions/9/OSGI-INF/MANIFEST.MF",
                "META-INF/versions/9/module-info.class",
                "META-INF/DEPENDENCIES",
                "META-INF/LICENSE*",
                "META-INF/NOTICE*",
                "META-INF/*.kotlin_module",
            )
        }
    }
    lint {
        abortOnError = false
    }
}

dependencies {
    implementation(project(":core"))
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
}
