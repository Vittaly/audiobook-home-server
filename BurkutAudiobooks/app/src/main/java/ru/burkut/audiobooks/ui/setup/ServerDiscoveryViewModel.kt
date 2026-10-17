package ru.burkut.audiobooks.ui.setup

import android.app.Application
import androidx.lifecycle.ViewModel
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.mdns.ServerDiscovery
import java.io.IOException
import java.util.concurrent.TimeUnit

class ServerDiscoveryViewModel(app: Application) : ViewModel() {

    private val discovery = ServerDiscovery(app)

    val servers = discovery.servers
    val status = discovery.status

    init {
        start()
    }

    fun start() = discovery.start()

    fun stop() = discovery.stop()

    override fun onCleared() {
        discovery.stop()
        super.onCleared()
    }
}

/**
 * Отдельный клиент для health-пробы. Не зависит от ApiProvider и settings —
 * проверяет именно тот URL, который передал пользователь.
 * Короткие таймауты, чтобы UI не «висел» на недоступном адресе.
 */
private val probeClient: OkHttpClient = OkHttpClient.Builder()
    .connectTimeout(3, TimeUnit.SECONDS)
    .readTimeout(3, TimeUnit.SECONDS)
    .callTimeout(5, TimeUnit.SECONDS)
    .build()

private suspend fun probeHealth(baseUrl: String): Boolean = withContext(Dispatchers.IO) {
    val url = baseUrl.trimEnd('/') + "/health"
    val response = probeClient.newCall(Request.Builder().url(url).build()).execute()
    response.use { it.isSuccessful }
}

fun connectToServer(
    app: App,
    scope: CoroutineScope,
    url: String,
    onSuccess: () -> Unit = {},
    onError: (String) -> Unit = {},
) {
    scope.launch {
        try {
            // 1. СНАЧАЛА проба. settings ещё не тронуты — если упадёт,
            //    откатывать нечего, UI никуда не дёрнется.
            if (!probeHealth(url)) {
                throw IOException("сервер вернул ошибку на /health")
            }
            // 2. Только теперь сохраняем. Реактивная навигация
            //    переключит UI на каталог — по-настоящему.
            app.settings.saveUrl(url)
            onSuccess()
        } catch (e: Exception) {
            onError("Не удалось подключиться: ${e.message ?: e.javaClass.simpleName}")
        }
    }
}