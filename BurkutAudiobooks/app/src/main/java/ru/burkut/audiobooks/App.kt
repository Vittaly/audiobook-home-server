package ru.burkut.audiobooks

import android.app.Application
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.source.MediaSource
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.data.BookRepository
import ru.burkut.audiobooks.data.ServerMonitor
import ru.burkut.audiobooks.data.api.ApiProvider
import ru.burkut.audiobooks.data.db.AppDatabase
import ru.burkut.audiobooks.data.settings.Settings
import ru.burkut.audiobooks.notifications.BookNotificationHelper
import ru.burkut.audiobooks.player.BookStorage
import ru.burkut.audiobooks.player.CoverStore
import ru.burkut.audiobooks.player.DownloadQueue
import ru.burkut.audiobooks.player.LocalBookDataSourceFactory

class App : Application() {

    lateinit var settings: Settings
        private set

    lateinit var db: AppDatabase
        private set

    lateinit var apiProvider: ApiProvider
        private set

    lateinit var repository: BookRepository
        private set

    lateinit var storage: BookStorage
        private set

    /** Локальный кэш обложек книг/раздач (для показа в оффлайне). */
    lateinit var coverStore: CoverStore
        private set

    lateinit var downloadQueue: DownloadQueue
        private set

    lateinit var serverMonitor: ServerMonitor
        private set

    lateinit var notificationHelper: BookNotificationHelper
        private set

    /** DataSource.Factory для локальных файлов книг (распознаёт URI /books/{id}/{ordinal}.mp3). */
    lateinit var localDataSourceFactory: LocalBookDataSourceFactory
        private set

    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    @OptIn(UnstableApi::class)
    override fun onCreate() {
        super.onCreate()

        // 1. Публикуем статическую ссылку первой строкой — appInstance()
        //    должен работать из любого места процесса сразу после этого.
        install(this)

        // 2. Остальная инициализация.
        settings = Settings(this)
        db = AppDatabase.get(this)
        apiProvider = ApiProvider(settings)
        repository = BookRepository(apiProvider, db.playbackDao(), db.catalogDao())

        storage = BookStorage(this)
        coverStore = CoverStore(apiProvider, repository, storage)
        downloadQueue = DownloadQueue(repository, storage, coverStore)
        notificationHelper = BookNotificationHelper(this)
        localDataSourceFactory = LocalBookDataSourceFactory(
            resolveFile = { bookHash, ordinal -> storage.playbackFile(bookHash, ordinal) },
            // Источник знает доступность сервера и оффлайн не ждёт файлов,
            // которых нет и не появятся.
            isOnline = {
                serverMonitor.availability.value ==
                    ru.burkut.audiobooks.data.ServerAvailability.Online
            },
        )

        serverMonitor = ServerMonitor(this, settings, repository)

        storage.migrateAndClean(this)

        // При появлении доступа к серверу синхронизируем каталог и догружаем обложки.
        serverMonitor.onServerOnline = { baseUrl ->
            appScope.launch {
                downloadQueue.setServerAvailable(true)
                // Догружаем обложки уже скачанных книг: книги, загруженные до
                // появления кэша обложек, тоже должны показываться оффлайн.
                coverStore.warmUpReadyBooks()
                // Синхронизируем каталог при возвращении сервера.
                runCatching { repository.syncBooks() }
            }
        }

        // Сервер пропал в процессе работы — ставим очередь загрузки на паузу.
        // Плеер и источник читают доступность динамически, так что оффлайн
        // воспроизведение продолжит работать с локальными файлами.
        serverMonitor.onServerOffline = {
            downloadQueue.setServerAvailable(false)
        }

        // «Книга готова» — уведомление по завершении загрузки книги на устройство.
        appScope.launch {
            downloadQueue.onBookReady.collect { pair ->
                pair?.let { (_, title) -> notificationHelper.notifyBookReady(title) }
            }
        }

        serverMonitor.start()
    }

    /** MediaSource.Factory для локальных файлов — используется PlayerService. */
    @OptIn(UnstableApi::class)
    fun localMediaSourceFactory(): MediaSource.Factory =
        ProgressiveMediaSource.Factory(localDataSourceFactory)

    companion object {
        @Volatile
        private var _instance: App? = null

        /**
         * Тот же объект, что `application` в Activity /
         * `LocalContext.current.applicationContext`.
         * Читается через `ru.burkut.audiobooks.ui.appInstance()`.
         */
        val instance: App
            get() = _instance
                ?: error("App ещё не инициализирован (onCreate не вызван)")

        internal fun install(app: App) {
            _instance = app
        }
    }
}