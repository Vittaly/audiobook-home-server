@file:androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)

package ru.burkut.audiobooks.player

import android.app.Application
import android.content.ComponentName
import android.net.Uri
import androidx.core.content.ContextCompat
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import androidx.media3.common.MediaItem
import androidx.media3.common.MediaMetadata
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.session.MediaController
import androidx.media3.session.SessionToken
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.suspendCancellableCoroutine
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.ChapterEntity
import java.io.File
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

data class PlaybackUiState(
    val isServerOnline: Boolean = false,
    val hasMedia: Boolean = false,
    val isPlaying: Boolean = false,
    val isBuffering: Boolean = false,
    val title: String? = null,          // книга
    val artist: String? = null,
    val artUri: String? = null,
    val positionMs: Long = 0L,          // позиция в текущей главе
    val durationMs: Long = 0L,          // длительность текущей главы
    val bufferedMs: Long = 0L,
    val bookPositionMs: Long = 0L,      // позиция в книге (верхний бар)
    val bookDurationMs: Long = 0L,      // общая длительность книги (верхний бар)
    val fileName: String? = null,       // имя текущего файла
    val mediaId: String? = null,        // "bookId:ordinal"
    val preparing: Boolean = false,
    val offlineQueued: Boolean = false, // книга в очереди (нет доступа к серверу)
)

class PlayerViewModel(application: Application) : ViewModel() {

    private val context = application.applicationContext
    private val app = application as App
    private val repository = app.repository
    private val settings = app.settings
    private val storage = app.storage
    private val downloadQueue = app.downloadQueue

    private val _state = MutableStateFlow(PlaybackUiState())
    val state: StateFlow<PlaybackUiState> = _state.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val _requestNextVolume = MutableStateFlow<BookEntity?>(null)
    val requestNextVolume: StateFlow<BookEntity?> = _requestNextVolume.asStateFlow()

    private var prepareJob: Job? = null
    private var retryJob: Job? = null

    private val controllerFuture: com.google.common.util.concurrent.ListenableFuture<MediaController> =
        MediaController.Builder(
            context,
            SessionToken(context, ComponentName(context, PlayerService::class.java)),
        ).buildAsync()

    private val controllerOrNull: MediaController?
        get() = if (controllerFuture.isDone) {
            runCatching { controllerFuture.get() }.getOrNull()
        } else null

    init {
        val executor = ContextCompat.getMainExecutor(context)
        controllerFuture.addListener(
            {
                runCatching { controllerFuture.get() }
                    .onSuccess { it.addListener(controllerListener) }
                snapshot()
            },
            executor,
        )
        viewModelScope.launch {
            // Отслеживаем доступность сервера для индикатора и очереди.
            app.serverMonitor.availability.collect { avail ->
                val online = avail == ru.burkut.audiobooks.data.ServerAvailability.Online
                _state.update { it.copy(isServerOnline = online) }
            }
        }
        viewModelScope.launch {
            while (true) {
                delay(400)
                snapshot()
            }
        }
        startProgressSaver()
    }

    private fun startProgressSaver() {
        viewModelScope.launch {
            while (isActive) {
                delay(SAVE_PROGRESS_INTERVAL_MS)
                saveCurrentProgress()
            }
        }
    }

    private fun saveCurrentProgress() {
        val controller = controllerOrNull ?: return
        val mediaId = controller.currentMediaItem?.mediaId ?: return
        val parsed = parseMediaId(mediaId) ?: return
        val (bookId, chapterOrdinal) = parsed
        val position = controller.currentPosition
        if (position > 0) {
            viewModelScope.launch {
                runCatching { repository.saveProgress(bookId, chapterOrdinal, position) }
            }
        }
    }

    private fun parseMediaId(mediaId: String?): Pair<Int, Int>? {
        if (mediaId == null) return null
        val parts = mediaId.split(":")
        if (parts.size != 2) return null
        val bookId = parts[0].toIntOrNull() ?: return null
        val chapterOrdinal = parts[1].toIntOrNull() ?: return null
        return bookId to chapterOrdinal
    }

    private val controllerListener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) {
            snapshot()
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            if (!isPlaying) saveCurrentProgress()
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            saveCurrentProgress()
            if (reason == Player.MEDIA_ITEM_TRANSITION_REASON_AUTO) {
                // Завершилась последняя глава текущей книги — предложим следующий том.
                maybeRequestNextVolume()
            }
        }

        override fun onPlaybackStateChanged(playbackState: Int) {
            if (playbackState == Player.STATE_ENDED) {
                saveCurrentProgress()
                val mediaId = controllerOrNull?.currentMediaItem?.mediaId
                if (parseMediaId(mediaId)?.first != null) maybeRequestNextVolume()
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            if (_state.value.preparing) return
            _state.update { it.copy(preparing = false) }
            _error.value = "Ошибка воспроизведения: ${error.message}"
        }
    }

    private suspend fun awaitController(): MediaController =
        suspendCancellableCoroutine { cont ->
            val executor = ContextCompat.getMainExecutor(context)
            controllerFuture.addListener(
                {
                    runCatching { controllerFuture.get() }
                        .onSuccess { cont.resume(it) }
                        .onFailure { cont.resumeWithException(it) }
                },
                executor,
            )
        }

    private fun snapshot() {
        val controller = controllerOrNull ?: return
        val item = controller.currentMediaItem
        val duration = controller.duration
        _state.update { prev ->
            if (prev.preparing) {
                prev.copy(
                    isPlaying = controller.playWhenReady && controller.isPlaying,
                    isBuffering = controller.playbackState == Player.STATE_BUFFERING,
                )
            } else {
                prev.copy(
                    hasMedia = controller.mediaItemCount > 0,
                    isPlaying = controller.playWhenReady && controller.isPlaying,
                    isBuffering = controller.playbackState == Player.STATE_BUFFERING,
                    title = item?.mediaMetadata?.artist?.toString(),      // книга
                    artist = item?.mediaMetadata?.title?.toString(),      // глава
                    artUri = item?.mediaMetadata?.artworkUri?.toString(),
                    positionMs = controller.currentPosition,
                    durationMs = if (duration > 0) duration else 0L,
                    bufferedMs = controller.bufferedPosition,
                    mediaId = item?.mediaId,
                    // Имя файла === номер главы на диске: {ordinal}.mp3
                    fileName = parseMediaId(item?.mediaId)?.let { (_, ordinal) -> "$ordinal.mp3" },
                    preparing = false,
                )
            }
        }
        viewModelScope.launch { updateBookProgress() }
    }

    /** Пересчёт позиции/длительности всей книги (верхний бар). */
    private suspend fun updateBookProgress() {
        val mediaId = controllerOrNull?.currentMediaItem?.mediaId ?: run {
            _state.update { it.copy(bookPositionMs = 0, bookDurationMs = 0) }
            return
        }
        val parsed = parseMediaId(mediaId) ?: return
        val (bookId, currentOrdinal) = parsed
        val chapters = repository.getChapters(bookId)
        if (chapters.isEmpty()) return

        val totalDurationSec = chapters.sumOf { (it.durationSec ?: 0).toLong() }
        val totalMs = totalDurationSec * 1000L

        // Позиция книги: сумма длительностей пройденных глав + позиция в текущей.
        val controller = controllerOrNull
        val currentPos = controller?.currentPosition ?: 0L
        var bookPos = 0L
        for (ch in chapters) {
            if (ch.ordinal < currentOrdinal) bookPos += (ch.durationSec ?: 0) * 1000L
        }
        bookPos += currentPos
        _state.update { it.copy(bookPositionMs = bookPos, bookDurationMs = totalMs) }
    }

    // ============================================================
    // Публичное API
    // ============================================================

    fun playBook(bookId: Int) = startPrepare(bookId, null)

    fun playChapter(bookId: Int, chapterOrdinal: Int) = startPrepare(bookId, chapterOrdinal)

    fun dismissNextVolume() = _requestNextVolume.update { null }

    fun startNextVolume(nextBookId: Int) {
        _requestNextVolume.update { null }
        playBook(nextBookId)
    }

    private fun startPrepare(bookId: Int, chapterOrdinal: Int?) {
        saveCurrentProgress()

        prepareJob?.cancel()
        retryJob?.cancel()

        prepareJob = viewModelScope.launch {
            try {
                prepareAndPlay(bookId, chapterOrdinal)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                _state.update { s -> s.copy(preparing = false) }
                _error.value = e.message ?: "Ошибка воспроизведения"
            }
        }
    }

    fun togglePlayPause() {
        val controller = controllerOrNull ?: return
        if (controller.playWhenReady) controller.pause() else controller.play()
    }

    fun next() {
        saveCurrentProgress()
        controllerOrNull?.seekToNext()
    }

    fun previous() {
        saveCurrentProgress()
        controllerOrNull?.seekToPrevious()
    }

    fun seekTo(positionMs: Long) {
        controllerOrNull?.seekTo(positionMs)
        saveCurrentProgress()
    }

    fun dismissError() = _error.update { null }

    override fun onCleared() {
        val controller = controllerOrNull
        val parsed = parseMediaId(controller?.currentMediaItem?.mediaId)
        val position = controller?.currentPosition ?: 0L
        if (parsed != null && position > 0) {
            val (bookId, chapterOrdinal) = parsed
            runBlocking {
                runCatching { repository.saveProgress(bookId, chapterOrdinal, position) }
            }
        }

        prepareJob?.cancel()
        retryJob?.cancel()

        if (controllerFuture.isDone) {
            runCatching { controllerFuture.get().release() }
        } else {
            controllerFuture.cancel(true)
        }
        super.onCleared()
    }

    // ============================================================
    // Ядро
    // ============================================================

    private suspend fun prepareAndPlay(bookId: Int, chapterOrdinal: Int?) {
        controllerOrNull?.let { c ->
            c.stop()
            c.clearMediaItems()
        }

        _state.update { PlaybackUiState(isServerOnline = it.isServerOnline, preparing = true) }

        // Получаем главы из локального кэша.
        val chapters = repository.getChapters(bookId)
        if (chapters.isEmpty()) {
            // Главы ещё не в кэше: попробуем подтянуть с сервера.
            val online = app.serverMonitor.availability.value ==
                ru.burkut.audiobooks.data.ServerAvailability.Online
            if (online) {
                val detail = repository.getReleaseDetailForBook(bookId, online)
                // После синка должны появиться главы.
            }
            val chapters2 = repository.getChapters(bookId)
            if (chapters2.isNotEmpty()) {
                return playLocal(bookId, chapters2, chapterOrdinal)
            }
        } else {
            return playLocal(bookId, chapters, chapterOrdinal)
        }

        // Нет глав локально и нет сервера — ставим раздачу в очередь.
        queueReleaseForBook(bookId)
    }

    private suspend fun playLocal(bookId: Int, chapters: List<ChapterEntity>, chapterOrdinal: Int?) {
        // Докачиваем те главы, которых нет локально.
        ensureChaptersDownloaded(bookId, chapters)

        val book = repository.getBookEntity(bookId)
        val coverUri = book?.coverUrl?.let { Uri.parse(it) }

        // Для растущих .part-файлов сообщаем фабрике целевой размер главы —
        // GrowingFileDataSource должен знать, когда глава закончится.
        for (ch in chapters) {
            app.localDataSourceFactory.setTargetLength(bookId, ch.ordinal, ch.sizeBytes)
        }

        val items = chapters.map { ch ->
            MediaItem.Builder()
                .setMediaId(chapterMediaId(bookId, ch.ordinal))
                .setUri(Uri.fromFile(storage.playbackFile(bookId, ch.ordinal)))
                .setMediaMetadata(
                    MediaMetadata.Builder()
                        .setTitle(ch.title ?: "Глава ${ch.ordinal}")
                        .setArtist(book?.title ?: "")
                        .setArtworkUri(coverUri)
                        .build(),
                )
                .build()
        }

        var index = 0
        var position = 0L
        if (chapterOrdinal != null) {
            index = chapters.indexOfFirst { it.ordinal == chapterOrdinal }.coerceAtLeast(0)
        } else {
            val saved = repository.getProgress(bookId)
            if (saved != null) {
                val idx = chapters.indexOfFirst { it.ordinal == saved.chapterOrdinal }
                if (idx >= 0) {
                    val ch = chapters[idx]
                    val nearEnd = ch.durationSec != null &&
                        saved.positionMs >= ch.durationSec * 1000L - 2000L
                    when {
                        nearEnd && idx + 1 < chapters.size -> { index = idx + 1; position = 0L }
                        nearEnd -> { index = idx; position = 0L }
                        else -> { index = idx; position = saved.positionMs }
                    }
                }
            }
        }

        val controller = awaitController()
        controller.setMediaItems(items, index, position)
        controller.prepare()
        controller.play()
        _state.update { it.copy(preparing = false) }
        updateBookProgress()
    }

    /** Докачивает недостающие главы через очередь (если сервер доступен). */
    private suspend fun ensureChaptersDownloaded(bookId: Int, chapters: List<ChapterEntity>) {
        val online = app.serverMonitor.availability.value ==
            ru.burkut.audiobooks.data.ServerAvailability.Online
        if (!online) {
            // Нет доступа: если книга частично есть — играем то, что есть;
            // недостающее поставлено в очередь в DownloadQueue через ServerMonitor.
            return
        }
        val missing = chapters.filterNot { storage.isChapterReady(it) }
        for (ch in missing) {
            val url = absoluteUrl(settings.baseUrlOrNull() ?: "", "/books/$bookId/stream/${ch.id}")
            downloadQueue.enqueue(
                DownloadTask(
                    bookId = bookId,
                    chapterId = ch.id,
                    ordinal = ch.ordinal,
                    sizeBytes = ch.sizeBytes,
                    url = url,
                ),
            )
        }
        // Даём очереди начать качать первую главу, чтобы файл появился для GrowingFileDataSource.
        // Очередь работает в фоне — плеер читает из .part.
    }

    /** Книга недоступна оффлайн — ставим раздачу в фоновую очередь. */
    private suspend fun queueReleaseForBook(bookId: Int) {
        _state.update {
            it.copy(preparing = false, offlineQueued = true, title = "Книга в очереди")
        }
        // Помечаем раздачу для фоновой докачки при появлении доступа к серверу.
        val book = repository.getBookEntity(bookId)
        val releaseId = book?.releaseId
        if (releaseId != null) {
            repository.setReleaseQueued(releaseId, true)
        }
        // DownloadQueue начнёт качать раздачу, как только ServerMonitor сообщит online.
    }

    /** После окончания последней главы книги — предложить следующий том. */
    private fun maybeRequestNextVolume() {
        viewModelScope.launch {
            val mediaId = controllerOrNull?.currentMediaItem?.mediaId ?: return@launch
            val parsed = parseMediaId(mediaId) ?: return@launch
            val (bookId, _) = parsed
            val book = repository.getBookEntity(bookId) ?: return@launch
            val siblings = repository.getBooksByRelease(book.releaseId, bookId)
            // Следующим будет том со следующим порядковым номером.
            val next = siblings.firstOrNull { it.ordinal > book.ordinal }
                ?: siblings.firstOrNull()
                ?: return@launch
            _requestNextVolume.value = next
        }
    }

    companion object {
        fun chapterMediaId(bookId: Int, chapterOrdinal: Int): String = "$bookId:$chapterOrdinal"
        private const val SAVE_PROGRESS_INTERVAL_MS = 5_000L
    }
}

fun absoluteUrl(baseUrl: String, url: String): String =
    if (url.startsWith("http://") || url.startsWith("https://")) {
        url
    } else {
        baseUrl.trimEnd('/') + if (url.startsWith("/")) url else "/$url"
    }
