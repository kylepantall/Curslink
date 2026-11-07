import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("org.jetbrains.kotlin.jvm")
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions { jvmTarget.set(JvmTarget.JVM_17) }
}

dependencies {
    // TLS-PSK (ECDHE-PSK, TLS 1.2) with the tls-unique channel binding: the
    // platform's TLS (Conscrypt) dropped PSK, BouncyCastle's TLS API has it.
    api("org.bouncycastle:bctls-jdk18on:1.79")
    api("org.jetbrains.kotlinx:kotlinx-serialization-json:1.7.3")
    testImplementation(kotlin("test"))
    testImplementation("junit:junit:4.13.2")
}

tasks.test {
    // The interop test runs the real Python host (interop/host.py) when
    // python3 can import curslink; it says so and skips otherwise.
    environment("CURSLINK_REPO", rootProject.projectDir.parentFile.absolutePath)
    testLogging { events("passed", "skipped", "failed"); showStandardStreams = true; exceptionFormat = org.gradle.api.tasks.testing.logging.TestExceptionFormat.FULL }
}
