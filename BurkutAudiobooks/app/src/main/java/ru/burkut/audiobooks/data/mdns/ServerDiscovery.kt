package ru.burkut.audiobooks.data.mdns

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.concurrent.ConcurrentLinkedQueue

data class DiscoveredServer(
    val name: String,
    val host: String,
    val port: Int,
) {
    val url: String get() = "http://$host:$port/"
}

sealed interface DiscoveryStatus {
    data object Idle : DiscoveryStatus
    data object Searching : DiscoveryStatus
    data object Stopped : DiscoveryStatus
    data class Error(val message: String) : DiscoveryStatus
}

class ServerDiscovery(context: Context) {

    private val appContext = context.applicationContext
    private val nsdManager =
        appContext.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val wifiManager =
        appContext.getSystemService(Context.WIFI_SERVICE) as WifiManager

    private val _servers = MutableStateFlow<List<DiscoveredServer>>(emptyList())
    val servers: StateFlow<List<DiscoveredServer>> = _servers.asStateFlow()

    private val _status = MutableStateFlow<DiscoveryStatus>(DiscoveryStatus.Idle)
    val status: StateFlow<DiscoveryStatus> = _status.asStateFlow()

    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private val pendingResolve = ConcurrentLinkedQueue<NsdServiceInfo>()

    /** Последний известный NsdServiceInfo по имени — нужен для повторного резолва. */
    private val lastInfo = mutableMapOf<String, NsdServiceInfo>()

    @Volatile
    private var resolving = false

    private var multicastLock: WifiManager.MulticastLock? = null

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var refreshJob: Job? = null

    fun start() {
        if (discoveryListener != null) return

        acquireMulticastLock()
        clearServers()

        _status.value = DiscoveryStatus.Searching

        val listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) {
                _status.value = DiscoveryStatus.Searching
                startPeriodicRefresh()
            }

            override fun onServiceFound(info: NsdServiceInfo) {
                lastInfo[info.serviceName] = info
                enqueueResolve(info)
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                lastInfo.remove(info.serviceName)
                _servers.update { list -> list.filterNot { it.name == info.serviceName } }
            }

            override fun onDiscoveryStopped(serviceType: String) {
                _status.value = DiscoveryStatus.Stopped
            }

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                _status.value = DiscoveryStatus.Error("Ошибка обнаружения (код $errorCode)")
                discoveryListener = null
                releaseMulticastLock()
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) {
                _status.value = DiscoveryStatus.Error("Ошибка остановки поиска (код $errorCode)")
            }
        }

        discoveryListener = listener
        nsdManager.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
    }

    fun stop() {
        refreshJob?.cancel()
        refreshJob = null

        discoveryListener?.let { runCatching { nsdManager.stopServiceDiscovery(it) } }
        discoveryListener = null
        pendingResolve.clear()
        lastInfo.clear()
        clearServers()
        releaseMulticastLock()
        _status.value = DiscoveryStatus.Stopped
    }

    /** Сброс списка серверов — при старте и стопе, чтобы UI не показывал старый IP. */
    private fun clearServers() {
        _servers.value = emptyList()
    }

    // -------- multicast lock --------

    private fun acquireMulticastLock() {
        if (multicastLock?.isHeld == true) return
        multicastLock = wifiManager.createMulticastLock("audiobooks-mdns").apply {
            setReferenceCounted(false)
            runCatching { acquire() }
        }
    }

    private fun releaseMulticastLock() {
        multicastLock?.takeIf { it.isHeld }?.let { runCatching { it.release() } }
        multicastLock = null
    }

    // -------- resolve --------

    private fun enqueueResolve(info: NsdServiceInfo) {
        // Больше НЕ пропускаем по имени — сервис мог сменить IP,
        // и нам нужен свежий адрес. Резолв идемпотентен: addServer
        // перезапишет запись с тем же именем.
        val host = info.host?.hostAddress
        if (host != null) {
            addServer(info.serviceName, host, info.port)
            return
        }
        pendingResolve.add(info)
        resolveNext()
    }

    private fun resolveNext() {
        if (resolving) return
        val info = pendingResolve.poll() ?: return
        resolving = true
        nsdManager.resolveService(
            info,
            object : NsdManager.ResolveListener {
                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) {
                    resolving = false
                    resolveNext()
                }

                override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                    resolving = false
                    val host = serviceInfo.host?.hostAddress
                    if (host != null) {
                        addServer(serviceInfo.serviceName, host, serviceInfo.port)
                    }
                    resolveNext()
                }
            },
        )
    }

    /**
     * Периодически перерезолвливает уже известные сервисы.
     * NsdManager не всегда зовёт onServiceFound при смене IP —
     * этот цикл закрывает дыру.
     */
    private fun startPeriodicRefresh() {
        refreshJob?.cancel()
        refreshJob = scope.launch {
            while (isActive) {
                delay(REFRESH_INTERVAL_MS)
                lastInfo.values.toList().forEach { info ->
                    pendingResolve.add(info)
                }
                if (pendingResolve.isNotEmpty()) resolveNext()
            }
        }
    }

    /**
     * Перезаписываем по имени: смена IP не должна плодить дубликаты.
     * Один сервис = одна карточка, всегда с актуальным адресом.
     */
    private fun addServer(name: String, host: String, port: Int) {
        _servers.update { list ->
            list.filterNot { it.name == name } + DiscoveredServer(name, host, port)
        }
    }

    companion object {
        /** Тип сервиса. Точка на конце — по RFC 6763. */
        const val SERVICE_TYPE = "_audiobook._tcp."
        private const val REFRESH_INTERVAL_MS = 5_000L
    }
}