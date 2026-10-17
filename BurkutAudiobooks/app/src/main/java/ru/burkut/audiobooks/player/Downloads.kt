package ru.burkut.audiobooks.player

import androidx.media3.common.util.UnstableApi
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.offline.Download
import androidx.media3.exoplayer.offline.DownloadManager
import androidx.media3.exoplayer.offline.DownloadRequest
import androidx.media3.exoplayer.scheduler.Requirements
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.data.api.BookDetailOut
import ru.burkut.audiobooks.data.api.ChapterOut
import java.util.concurrent.Executors

@UnstableApi
class BookDownloads(
    private val downloadManager: DownloadManager,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    fun enqueueBook(book: BookDetailOut, baseUrl: String) {
        scope.launch {
            book.chapters.forEach { chapter ->
                val request = DownloadRequest.Builder(
                    PlayerViewModel.chapterMediaId(book.id, chapter.id),
                    android.net.Uri.parse(absoluteUrl(baseUrl, chapter.stream_url)),
                ).build()
                downloadManager.addDownload(request)
            }
        }
    }

    fun removeBook(book: BookDetailOut) {
        scope.launch {
            book.chapters.forEach { chapter ->
                downloadManager.removeDownload(PlayerViewModel.chapterMediaId(book.id, chapter.id))
            }
        }
    }
}

@UnstableApi
class DownloadObserver(
    private val downloadManager: DownloadManager,
) : DownloadManager.Listener {

    private val _downloads = MutableStateFlow<Map<String, Download>>(emptyMap())
    val downloads: StateFlow<Map<String, Download>> = _downloads.asStateFlow()

    init {
        downloadManager.addListener(this)
        refresh()
    }

    private fun refresh() {
        _downloads.update {
            downloadManager.currentDownloads.associateBy { d -> d.request.id }
        }
    }

    override fun onInitialized(downloadManager: DownloadManager) = refresh()

    override fun onDownloadChanged(
        downloadManager: DownloadManager,
        download: Download,
        error: Exception?,
    ) = refresh()

    override fun onDownloadRemoved(downloadManager: DownloadManager, download: Download) = refresh()

    override fun onWaitingForRequirementsChanged(
        downloadManager: DownloadManager,
        waitingForRequirements: Boolean,
    ) = Unit

    fun release() {
        downloadManager.removeListener(this)
    }
}

@UnstableApi
fun createDownloads(
    context: android.content.Context,
    databaseProvider: StandaloneDatabaseProvider,
    cache: SimpleCache,
): DownloadManager =
    DownloadManager(
        context,
        databaseProvider,
        cache,
        DefaultHttpDataSource.Factory(),
        Executors.newFixedThreadPool(DEFAULT_DOWNLOAD_THREADS),
    ).apply {
        requirements = Requirements(Requirements.NETWORK)
        setMaxParallelDownloads(DEFAULT_PARALLEL_DOWNLOADS)
    }

@OptIn(UnstableApi::class)
internal fun chapterCacheProgress(download: Download?): Float {
    if (download == null) return 0f
    val total = download.contentLength
    if (total <= 0L) return 0f
    return (download.bytesDownloaded.toFloat() / total).coerceIn(0f, 1f)
}

@OptIn(UnstableApi::class)
internal fun isChapterCached(download: Download?): Boolean =
    download != null && download.state == Download.STATE_COMPLETED

@OptIn(UnstableApi::class)
internal fun isChapterDownloading(download: Download?): Boolean =
    download != null &&
        (download.state == Download.STATE_DOWNLOADING || download.state == Download.STATE_QUEUED)

private const val DEFAULT_DOWNLOAD_THREADS = 4
private const val DEFAULT_PARALLEL_DOWNLOADS = 4
