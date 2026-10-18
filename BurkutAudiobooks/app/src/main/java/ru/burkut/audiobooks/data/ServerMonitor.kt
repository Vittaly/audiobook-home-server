package ru.burkut.audiobooks.data

import android.content.Context
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.data.mdns.DiscoveredServer
import ru.burkut.audiobooks.data.mdns.ServerDiscovery
import ru.burkut.audiobooks.data.settings.Settings

/** Доступность сервера в локальной сети. */
enum class ServerAvailability {
    /** Сервер не настроен и не найден. */
    Unknown,
    /** Сервер доступен. */
    Online,
    /** Сервер недоступен (нет доступа в локальной сети). */
    Offline,
}

/**
 * Авто-подключение + мониторинг доступности сервера.
 *
 * - Если адрес сервера уже сохранён — периодически пробует `/health`.
 * - Параллельно в фоне ищет сервер по mDNS (`_audiobook._tcp.`).
 * - Если найден новый или выбранный сервер подтверждается `/health`,
 *   адрес сохраняется в Settings автоматически (без диалогов первого запуска).
 * - Публикует [availability] — по нему загорается зелёный индикатор
 *   и управляется очередь фоновой загрузки.
 */
class ServerMonitor(
    private val context: Context,
    private val settings: Settings,
    private val repository: BookRepository,
) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val discovery = ServerDiscovery(context)

    private val _availability = MutableStateFlow<ServerAvailability>(
        if (settings.baseUrlOrNull() == null) ServerAvailability.Unknown else ServerAvailability.Offline,
    )
    val availability: StateFlow<ServerAvailability> = _availability.asStateFlow()

    private var probeJob: Job? = null

    fun start() {
        discovery.start()
        startProbing()
    }

    fun stop() {
        probeJob?.cancel()
        discovery.stop()
    }

    private fun startProbing() {
        probeJob?.cancel()
        probeJob = scope.launch {
            // Начинаем с немедленной пробы, затем циклически.
            while (true) {
                probe()
                delay(PROBE_INTERVAL_MS)
            }
        }
    }

    /** Одиночная проба: mDNS-сервера + сохранённый адрес. */
    private suspend fun probe() {
        val saved = settings.baseUrlOrNull()
        val discovered = discovery.servers.value

        // 1. Пробуем сохранённый адрес.
        if (saved != null) {
            if (repository.probeHealth()) {
                if (_availability.value != ServerAvailability.Online) {
                    _availability.value = ServerAvailability.Online
                    onOnline(saved)
                }
                return
            }
        }

        // 2. Пробуем найденные по mDNS сервера.
        for (server in discovered) {
            if (server.host == hostOf(saved)) continue
            if (probeServer(server)) {
                settings.saveUrl(server.url)
                if (_availability.value != ServerAvailability.Online) {
                    _availability.value = ServerAvailability.Online
                    onOnline(server.url)
                }
                return
            }
        }

        // 3. Никого не нашли — оффлайн.
        if (_availability.value != ServerAvailability.Offline) {
            _availability.value = ServerAvailability.Offline
            onOffline()
        }
    }

    private suspend fun probeServer(server: DiscoveredServer): Boolean {
        val old = settings.baseUrlOrNull()
        settings.saveUrl(server.url)
        val ok = repository.probeHealth()
        // Возвращаем старый адрес, если он был (чтобы не трогать настройку при неудаче).
        if (!ok && old != null) settings.saveUrl(old)
        return ok
    }

    private fun hostOf(url: String?): String? {
        if (url == null) return null
        return runCatching {
            // url вида http://host:port/
            val trimmed = url.removePrefix("http://").removePrefix("https://").removeSuffix("/")
            trimmed.substringBefore(":")
        }.getOrNull()
    }

    private fun onOnline(baseUrl: String) {
        // Хук для будущей фоновой загрузки очереди (переопределяется в App/DownloadQueue).
        onServerOnline?.invoke(baseUrl)
    }

    private fun onOffline() {
        // Хук для паузы фоновой загрузки при потере сервера.
        onServerOffline?.invoke()
    }

    /** Вызывается, когда сервер стал доступен. Устанавливается App.kt. */
    var onServerOnline: ((String) -> Unit)? = null

    /** Вызывается, когда сервер стал недоступен. Устанавливается App.kt. */
    var onServerOffline: (() -> Unit)? = null

    companion object {
        private const val PROBE_INTERVAL_MS = 10_000L
    }
}
