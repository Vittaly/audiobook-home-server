package ru.burkut.audiobooks

import android.app.Application
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.datasource.cache.CacheDataSource
import ru.burkut.audiobooks.data.BookRepository
import ru.burkut.audiobooks.data.api.ApiProvider
import ru.burkut.audiobooks.data.db.AppDatabase
import ru.burkut.audiobooks.data.settings.Settings
import ru.burkut.audiobooks.player.BookDownloads
import ru.burkut.audiobooks.player.DownloadObserver
import ru.burkut.audiobooks.player.createDownloads
import java.io.File

class App : Application() {

    lateinit var settings: Settings
        private set

    lateinit var db: AppDatabase
        private set

    lateinit var apiProvider: ApiProvider
        private set

    lateinit var repository: BookRepository
        private set

    lateinit var simpleCache: SimpleCache
        private set

    lateinit var bookDownloads: BookDownloads
        private set

    lateinit var downloadObserver: DownloadObserver
        private set

    private lateinit var databaseProvider: StandaloneDatabaseProvider

    @OptIn(UnstableApi::class)
    override fun onCreate() {
        super.onCreate()

        // 1. Публикуем статическую ссылку первой строкой — appInstance()
        //    должен работать из любого места процесса сразу после этого.
        install(this)

        // 2. Остальная инициализация — как было.
        settings = Settings(this)
        db = AppDatabase.get(this)
        apiProvider = ApiProvider(settings)
        repository = BookRepository(apiProvider, db.playbackDao())

        databaseProvider = StandaloneDatabaseProvider(this)
        simpleCache = SimpleCache(
            File(filesDir, "audio-cache"),
            LeastRecentlyUsedCacheEvictor(MAX_CACHE_BYTES),
            databaseProvider,
        )

        val downloadManager = createDownloads(this, databaseProvider, simpleCache)
        downloadObserver = DownloadObserver(downloadManager)
        bookDownloads = BookDownloads(downloadManager)
    }

    @OptIn(UnstableApi::class)
    fun mediaSourceFactory(): DefaultMediaSourceFactory =
        DefaultMediaSourceFactory(
            CacheDataSource.Factory()
                .setCache(simpleCache)
                .setUpstreamDataSourceFactory(DefaultHttpDataSource.Factory()),
        )

    companion object {
        private const val MAX_CACHE_BYTES = 512L * 1024 * 1024

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