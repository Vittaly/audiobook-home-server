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
import androidx.media3.datasource.HttpDataSource
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
import ru.burkut.audiobooks.data.api.BookDetailOut
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.api.ChapterOut
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

data class PlaybackUiState(
    val isConnected: Boolean = false,
    val hasMedia: Boolean = false,
    val isPlaying: Boolean = false,
    val isBuffering: Boolean = false,
    val title: String? = null,
    val artist: String? = null,
    val artUri: String? = null,
    val positionMs: Long = 0L,
    val durationMs: Long = 0L,
    val bufferedMs: Long = 0L,
    val mediaId: String? = null,
    val preparing: Boolean = false,
)

class PlayerViewModel(application: Application) : ViewModel() {

    private val context = application.applicationContext
    private val app = application as App
    private val repository = app.repository
    private val settings = app.settings

    private val _state = MutableStateFlow(PlaybackUiState())
    val state: StateFlow<PlaybackUiState> = _state.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private var retryCount = 0

    private var prepareJob: Job? = null
    private var retryJob: Job? = null
    private var progressSaverJob: Job? = null

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
            while (true) {
                delay(400)
                snapshot()
            }
        }
        startProgressSaver()
    }

    // ============================================================
    // Сохранение позиции
    // ============================================================

    private fun startProgressSaver() {
        progressSaverJob?.cancel()
        progressSaverJob = viewModelScope.launch {
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
        val (bookId, chapterId) = parsed
        val position = controller.currentPosition
        if (position <= 0) return

        viewModelScope.launch {
            runCatching { repository.saveProgress(bookId, chapterId, position) }
        }
    }

    private fun parseMediaId(mediaId: String?): Pair<Int, Int>? {
        if (mediaId == null) return null
        val parts = mediaId.split(":")
        if (parts.size != 2) return null
        val bookId = parts[0].toIntOrNull() ?: return null
        val chapterId = parts[1].toIntOrNull() ?: return null
        return bookId to chapterId
    }

    // ============================================================
    // Listener
    // ============================================================

    private val controllerListener = object : Player.Listener {
        override fun onEvents(player: Player, events: Player.Events) {
            snapshot()
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            if (!isPlaying) saveCurrentProgress()
        }

        override fun onMediaItemTransition(mediaItem: MediaItem?, reason: Int) {
            saveCurrentProgress()
        }

        override fun onPlaybackStateChanged(playbackState: Int) {
            if (playbackState == Player.STATE_ENDED) {
                saveCurrentProgress()
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            if (_state.value.preparing) return

            val code = (error.cause as? HttpDataSource.InvalidResponseCodeException)?.responseCode
            val transient = code == 409 || code == 502 || code == 503

            if (transient && retryCount < MAX_RETRIES) {
                retryCount++
                _state.update { it.copy(preparing = true) }
                retryJob?.cancel()
                retryJob = viewModelScope.launch {
                    delay(RETRY_DELAY_MS)
                    controllerOrNull?.let { c ->
                        c.prepare()
                        c.play()
                    }
                }
            } else {
                _state.update { it.copy(preparing = false) }
                _error.value = error.message ?: "Ошибка воспроизведения"
            }
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
                    isConnected = true,
                    isPlaying = controller.playWhenReady && controller.isPlaying,
                    isBuffering = controller.playbackState == Player.STATE_BUFFERING,
                )
            } else {
                PlaybackUiState(
                    isConnected = true,
                    hasMedia = controller.mediaItemCount > 0,
                    isPlaying = controller.playWhenReady && controller.isPlaying,
                    isBuffering = controller.playbackState == Player.STATE_BUFFERING,
                    title = item?.mediaMetadata?.title?.toString(),
                    artist = item?.mediaMetadata?.artist?.toString(),
                    artUri = item?.mediaMetadata?.artworkUri?.toString(),
                    positionMs = controller.currentPosition,
                    durationMs = if (duration > 0) duration else 0L,
                    bufferedMs = controller.bufferedPosition,
                    mediaId = item?.mediaId,
                    preparing = false,
                )
            }
        }
    }

    // ============================================================
    // Публичное API
    // ============================================================

    fun playBook(bookId: Int) = startPrepare(bookId, null)

    fun playChapter(bookId: Int, chapterId: Int) = startPrepare(bookId, chapterId)

    private fun startPrepare(bookId: Int, chapterId: Int?) {
        // Перед сменой книги сохраняем позицию того, что играло.
        saveCurrentProgress()

        prepareJob?.cancel()
        retryJob?.cancel()
        retryJob = null

        prepareJob = viewModelScope.launch {
            try {
                prepareAndPlay(bookId, chapterId)
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
        // viewModelScope уже отменён, сохраняем синхронно (это короткая
        // запись одной строки в Room).
        val controller = controllerOrNull
        val parsed = parseMediaId(controller?.currentMediaItem?.mediaId)
        val position = controller?.currentPosition ?: 0L
        if (parsed != null && position > 0) {
            val (bookId, chapterId) = parsed
            runBlocking {
                runCatching { repository.saveProgress(bookId, chapterId, position) }
            }
        }

        prepareJob?.cancel()
        retryJob?.cancel()
        progressSaverJob?.cancel()

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

    private suspend fun prepareAndPlay(bookId: Int, chapterId: Int?) {
        controllerOrNull?.let { c ->
            c.stop()
            c.clearMediaItems()
        }

        _state.update {
            PlaybackUiState(
                isConnected = it.isConnected,
                preparing = true,
                title = "Подготовка…",
            )
        }
        retryCount = 0

        val base = settings.baseUrlOrNull()
            ?: throw IllegalStateException("Сервер не настроен")

        runCatching { repository.startDownload(bookId) }

        var detail = repository.getBook(bookId)
        var attempts = 0
        while (detail.chapters.isEmpty()) {
            when (detail.status) {
                BookStatus.error -> {
                    _state.update { it.copy(preparing = false) }
                    throw IllegalStateException("Сервер не смог загрузить книгу")
                }
                BookStatus.deleted -> {
                    _state.update { it.copy(preparing = false) }
                    throw IllegalStateException("Книга удалена")
                }
                else -> Unit
            }

            _state.update {
                it.copy(
                    title = detail.title,
                    artist = when (detail.status) {
                        BookStatus.announced -> "Сервер разбирает торрент…"
                        BookStatus.downloading -> "Получаем список глав…"
                        BookStatus.partial -> "Получаем список глав…"
                        else -> "Подготовка…"
                    },
                )
            }

            delay(METADATA_POLL_MS)
            attempts++
            detail = runCatching { repository.getBook(bookId) }.getOrNull() ?: detail

            if (attempts > MAX_METADATA_ATTEMPTS) {
                _state.update { it.copy(preparing = false) }
                throw IllegalStateException(
                    "Сервер не вернул список глав. Проверьте статус загрузки.",
                )
            }
        }

        app.bookDownloads.enqueueBook(detail, base)

        // ---------- Восстановление позиции ----------
        val items = detail.chapters.map { chapter -> chapter.toMediaItem(detail, base) }
        var index = 0
        var position = 0L

        if (chapterId != null) {
            // Явно указана глава (тап по списку) — начинаем с неё.
            index = detail.chapters.indexOfFirst { it.id == chapterId }.coerceAtLeast(0)
        } else {
            // Продолжаем с сохранённого места (кнопка «Слушать»).
            val saved = repository.getProgress(bookId)
            if (saved != null) {
                val idx = detail.chapters.indexOfFirst { it.id == saved.chapterId }
                if (idx >= 0) {
                    val chapter = detail.chapters[idx]
                    val nearEnd = chapter.duration_sec != null &&
                            saved.positionMs >= chapter.duration_sec * 1000L - 2000L

                    when {
                        // Глава доиграна — начинаем следующую.
                        nearEnd && idx + 1 < detail.chapters.size -> {
                            index = idx + 1
                            position = 0L
                        }
                        // Доиграна последняя — начинаем её же с начала.
                        nearEnd -> {
                            index = idx
                            position = 0L
                        }
                        // Обычный случай: продолжаем с сохранённой позиции.
                        else -> {
                            index = idx
                            position = saved.positionMs
                        }
                    }
                }
            }
        }

        val controller = awaitController()
        controller.setMediaItems(items, index, position)
        controller.prepare()
        controller.play()

        _state.update { it.copy(preparing = false) }
    }

    companion object {
        fun chapterMediaId(bookId: Int, chapterId: Int): String = "$bookId:$chapterId"
        private const val MAX_RETRIES = 30
        private const val RETRY_DELAY_MS = 2_000L
        private const val METADATA_POLL_MS = 2_000L
        private const val MAX_METADATA_ATTEMPTS = 150
        private const val SAVE_PROGRESS_INTERVAL_MS = 5_000L
    }
}

internal fun ChapterOut.toMediaItem(book: BookDetailOut, baseUrl: String): MediaItem {
    val streamUrl = absoluteUrl(baseUrl, stream_url)
    val coverUri = book.cover_url
        ?.let { absoluteUrl(baseUrl, it) }
        ?.let { Uri.parse(it) }
    return MediaItem.Builder()
        .setMediaId(PlayerViewModel.chapterMediaId(book.id, id))
        .setUri(streamUrl)
        .setMediaMetadata(
            MediaMetadata.Builder()
                .setTitle(title ?: "Глава $ordinal")
                .setArtist(book.title)
                .setAlbumTitle(book.title)
                .setArtworkUri(coverUri)
                .build(),
        )
        .build()
}

fun absoluteUrl(baseUrl: String, url: String): String =
    if (url.startsWith("http://") || url.startsWith("https://")) {
        url
    } else {
        baseUrl.trimEnd('/') + if (url.startsWith("/")) url else "/$url"
    }