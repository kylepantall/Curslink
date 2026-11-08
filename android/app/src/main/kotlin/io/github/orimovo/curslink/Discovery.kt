package io.github.orimovo.curslink

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import java.util.concurrent.ConcurrentHashMap

/**
 * mDNS / DNS-SD, as curslink/pairing/discovery.py: advertises this phone as
 * a Curslink agent (so the computer's Devices page lists it under Nearby)
 * and keeps track of hosts it can see (to find the paired one's address).
 */
class Discovery(context: Context) {
    data class FoundHost(val id: String, val name: String, val address: String, val controlPort: Int)

    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val wifi = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
    private var multicast: WifiManager.MulticastLock? = null
    private var registration: NsdManager.RegistrationListener? = null
    private var discovery: NsdManager.DiscoveryListener? = null
    private val resolving = ArrayDeque<NsdServiceInfo>()
    private var busy = false
    val hosts = ConcurrentHashMap<String, FoundHost>()

    fun start(deviceId: String, name: String, pairingPort: Int) {
        stop()
        multicast = wifi.createMulticastLock("curslink-mdns").apply { setReferenceCounted(false); acquire() }
        val label = name.replace(Regex("[^A-Za-z0-9 _-]"), "").trim().take(40).ifEmpty { "Curslink" }
        val info = NsdServiceInfo().apply {
            serviceName = "$label (${deviceId.take(20)})"
            serviceType = TYPE
            port = pairingPort
            setAttribute("id", deviceId)
            setAttribute("name", name)
            setAttribute("role", "agent")
            setAttribute("v", "1")
        }
        val reg = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) { AppLog.i(TAG, "advertising as ${info.serviceName}") }
            override fun onRegistrationFailed(info: NsdServiceInfo, code: Int) { AppLog.w(TAG, "can't advertise: $code") }
            override fun onServiceUnregistered(info: NsdServiceInfo) {}
            override fun onUnregistrationFailed(info: NsdServiceInfo, code: Int) {}
        }
        registration = reg
        runCatching { nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, reg) }
        val disc = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(type: String) {}
            override fun onDiscoveryStopped(type: String) {}
            override fun onStartDiscoveryFailed(type: String, code: Int) { AppLog.w(TAG, "can't browse: $code") }
            override fun onStopDiscoveryFailed(type: String, code: Int) {}
            override fun onServiceFound(info: NsdServiceInfo) { queue(info) }
            override fun onServiceLost(info: NsdServiceInfo) {}
        }
        discovery = disc
        runCatching { nsd.discoverServices(TYPE, NsdManager.PROTOCOL_DNS_SD, disc) }
    }

    @Synchronized private fun queue(info: NsdServiceInfo) {
        resolving.addLast(info)
        next()
    }

    // NsdManager resolves one service at a time.
    @Synchronized private fun next() {
        if (busy) return
        val info = resolving.removeFirstOrNull() ?: return
        busy = true
        @Suppress("DEPRECATION")
        nsd.resolveService(info, object : NsdManager.ResolveListener {
            override fun onResolveFailed(info: NsdServiceInfo, code: Int) { done() }
            override fun onServiceResolved(info: NsdServiceInfo) {
                runCatching { take(info) }
                done()
            }
        })
    }

    @Synchronized private fun done() {
        busy = false
        next()
    }

    private fun take(info: NsdServiceInfo) {
        val attrs = info.attributes
        fun attr(k: String) = attrs[k]?.toString(Charsets.UTF_8) ?: ""
        if (attr("role") != "host") return
        val id = attr("id").ifEmpty { return }
        val cport = attr("cport").toIntOrNull() ?: return
        val address = if (Build.VERSION.SDK_INT >= 34) {
            info.hostAddresses.firstOrNull { it is java.net.Inet4Address }?.hostAddress ?: info.hostAddresses.firstOrNull()?.hostAddress
        } else {
            @Suppress("DEPRECATION") info.host?.hostAddress
        } ?: return
        hosts[id] = FoundHost(id, attr("name").ifEmpty { id }, address, cport)
        AppLog.i(TAG, "found host ${attr("name")} at $address:$cport")
    }

    fun stop() {
        registration?.let { runCatching { nsd.unregisterService(it) } }
        discovery?.let { runCatching { nsd.stopServiceDiscovery(it) } }
        registration = null
        discovery = null
        multicast?.let { runCatching { it.release() } }
        multicast = null
    }

    private companion object {
        const val TYPE = "_curslink._tcp."
        const val TAG = "CurslinkDiscovery"
    }
}
