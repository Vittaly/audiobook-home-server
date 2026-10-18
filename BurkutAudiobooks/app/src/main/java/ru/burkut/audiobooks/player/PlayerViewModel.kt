@file:androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)

package ru.burkut.audiobooks.player

import android.app.Application
import android.content.ComponentName
import android.net.Uri
import android.util.Log
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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
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

    /** Схлопывание частых пересчётов прогресса книги в один фоновый прогон. */
    private var bookProgressJob: Job? = null
    private var bookProgressPending = false

    /** Книга, открытая в полноэкранном плеере (для promote/demote очереди). */
    private var playerScreenBookId: Int? = null

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
                    // ExoPlayer периодически отдаёт C.TIME_UNSET (<= 0) при
                    // буферизации/переходе. Не сбрасываем длительность в 0 —
                    // иначе слайдер главы прыгает (мерцание). Держим последнее
                    // известное значение, пока играет тот же медиаэлемент.
                    durationMs = if (duration > 0) {
                        duration
                    } else if (item?.mediaId == prev.mediaId) {
                        prev.durationMs
                    } else {
                        0L
                    },
                    bufferedMs = controller.bufferedPosition,
                    mediaId = item?.mediaId,
                    // Имя файла === номер главы на диске: {ordinal}.mp3
                    fileName = parseMediaId(item?.mediaId)?.let { (_, ordinal) -> "$ordinal.mp3" },
                    preparing = false,
                )
            }
        }
        requestBookProgressUpdate()
    }

    /** Схлопывает частые вызовы пересчёта прогресса книги в один прогон. */
    private fun requestBookProgressUpdate() {
        if (bookProgressJob?.isActive == true) {
            bookProgressPending = true
            return
        }
        bookProgressJob = viewModelScope.launch {
            do {
                bookProgressPending = false
                updateBookProgress()
            } while (bookProgressPending && isActive)
        }
    }

    /** Пересчёт позиции/длительности всей книги (верхний бар). */
    private suspend fun updateBookProgress() {
        val controller = controllerOrNull
        val mediaId = controller?.currentMediaItem?.mediaId ?: run {
            _state.update { it.copy(bookPositionMs = 0, bookDurationMs = 0) }
            return
        }
        val parsed = parseMediaId(mediaId) ?: return
        val (bookId, currentOrdinal) = parsed
        val chapters = repository.getChapters(bookId)
        if (chapters.isEmpty()) return

        // Сервер не знает длительностей глав (durationSec == null для глав из
        // торрента) — приоритет у длительностей, измеренных клиентом по
        // аудиофайлу (localDurationSec). Уже скачанные файлы измеряем один раз.
        backfillLocalDurations(chapters)

        // Пока читали БД, могла произойти смена главы — устаревшие данные не пишем.
        if (controllerOrNull?.currentMediaItem?.mediaId != mediaId) return

        val durationOf = { ch: ChapterEntity -> (ch.localDurationSec ?: ch.durationSec ?: 0).toLong() }
        val totalMs = chapters.sumOf(durationOf) * 1000L

        // Позиция книги: сумма длительностей пройденных глав + позиция в текущей.
        val currentPos = controller?.currentPosition ?: 0L
        var bookPos = 0L
        for (ch in chapters) {
            if (ch.ordinal < currentOrdinal) bookPos += durationOf(ch) * 1000L
        }
        bookPos += currentPos

        // Длительность ИГРАЮЩЕЙ главы: пока она ещё растёт (.part) и ExoPlayer
        // отдаёт C.TIME_UNSET (0) — показываем измеренную localDurationSec. Уже
        // известную длительность нулём не перебиваем, чтобы слайдер не мерцал.
        val controllerDuration = controller?.duration ?: 0L
        val currentCh = chapters.firstOrNull { it.ordinal == currentOrdinal }
        val chapterMs = (currentCh?.localDurationSec ?: currentCh?.durationSec ?: 0) * 1000L
        val effectiveDuration = when {
            controllerDuration > 0 -> controllerDuration
            chapterMs > 0 -> chapterMs
            else -> 0L
        }
        _state.update {
            it.copy(
                bookPositionMs = bookPos,
                bookDurationMs = totalMs,
                durationMs = if (effectiveDuration > 0) effectiveDuration else it.durationMs,
            )
        }
    }

    /**
     * Измеряет реальные длительности уже скачанных глав, у которых ещё нет
     * локальной длительности (localDurationSec == null), и сохраняет в кэш.
     * Вызывается при каждом обновлении прогресса, но каждая глава измеряется
     * только один раз (после сохранения localDurationSec <> null повторно не
     * читается).
     */
    private suspend fun backfillLocalDurations(chapters: List<ChapterEntity>) {
        for (ch in chapters) {
            if (ch.localDurationSec != null) continue
            if (!storage.isChapterReady(ch)) continue
            // Измерение аудиофайла — синхронная работа с метаданными, выполняем
            // вне главного потока (вызов может прийти из UI-контекста).
            val sec = withContext(Dispatchers.IO) {
                ChapterDurationProbe.measureDurationSec(
                    storage.chapterFile(ch.bookId, ch.ordinal),
                )
            }
            if (sec != null && sec > 0) {
                repository.setChapterLocalDuration(ch.id, sec)
            }
        }
    }

    // ============================================================
    // Публичное API
    // ============================================================

    fun playBook(bookId: Int) = startPrepare(bookId, null)

    fun playChapter(bookId: Int, chapterOrdinal: Int) = startPrepare(bookId, chapterOrdinal)

    /**
     * Проигрывание ВСЕЙ раздачи с самого начала.
     *
     * Клиент не знает книг заранее: сервер не создаёт книгу-заглушку, книги
     * появляются только после старта загрузки. Стартуем торрент на уровне
     * раздачи (POST /releases/{id}/download), ждём появления книг, берём
     * ПЕРВУЮ по порядку (минимальный ordinal), ждём её глав и запускаем
     * плеер с первой главы — с самого начала раздачи.
     */
    fun playRelease(releaseId: Int) {
        Log.d(TAG, "playRelease releaseId=$releaseId")
        saveCurrentProgress()

        prepareJob?.cancel()
        retryJob?.cancel()

        prepareJob = viewModelScope.launch {
            try {
                startReleaseDownloadAndWait(releaseId)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "playRelease error releaseId=$releaseId", e)
                _state.update { s -> s.copy(preparing = false) }
                _error.value = e.message ?: "Ошибка воспроизведения"
            }
        }
    }

    /**
     * Запускает серверную загрузку ВСЕЙ раздачи (POST /releases/{id}/download —
     * книга не нужна, сервер не создаёт книгу-заглушку) и ждёт появления ПЕРВОЙ
     * книги по порядку (минимальный ordinal), затем её глав, и начинает
     * воспроизведение с самого начала — с первой главы первой книги.
     */
    private suspend fun startReleaseDownloadAndWait(releaseId: Int) {
        Log.d(TAG, "startReleaseDownloadAndWait releaseId=$releaseId")
        saveCurrentProgress()

        _state.update {
            it.copy(
                preparing = true,
                title = "Подготовка раздачи на сервере…",
                artist = null,
                hasMedia = false,
                offlineQueued = false,
            )
        }

        // Сообщаем серверу о начале загрузки всей раздачи (POST /releases/{id}/download).
        val started = runCatching {
            Log.d(TAG, "startReleaseDownload POST /releases/$releaseId/download")
            repository.startReleaseDownload(releaseId)
        }
        Log.d(TAG, "startReleaseDownload result failure=${started.isFailure} err=${started.exceptionOrNull()?.message}")
        if (started.isFailure && repository.getBooksByRelease(releaseId).isEmpty()) {
            // На случай «already_done» пробуем ещё раз подтянуть карточку раздачи.
            runCatching { repository.getReleaseDetail(releaseId, online = true) }
            if (repository.getBooksByRelease(releaseId).isEmpty()) {
                _state.update { it.copy(preparing = false) }
                _error.value = "Не удалось начать загрузку на сервере: " +
                    (started.exceptionOrNull()?.message ?: "неизвестная ошибка")
                return
            }
        }

        // Ждём появления ПЕРВОЙ книги по порядку (минимальный ordinal) и её глав.
        // Поллер на сервере создаёт книги только после фактического ингеста
        // файлов торрента, поэтому книга-«заглушка» тут не появится — ждём
        // настоящую книгу с её главами.
        val deadline = System.currentTimeMillis() + SERVER_PREPARE_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline && currentCoroutineContext().isActive) {
            runCatching { repository.getReleaseDetail(releaseId, online = true) }
            val firstBook = repository.getBooksByRelease(releaseId).minByOrNull { it.ordinal }
            if (firstBook != null) {
                val chapters = repository.getChapters(firstBook.id)
                Log.d(TAG, "startReleaseDownload poll firstBook=${firstBook.id} ordinal=${firstBook.ordinal} chapters=${chapters.size} elapsed=${System.currentTimeMillis() - (deadline - SERVER_PREPARE_TIMEOUT_MS)}ms")
                if (chapters.isNotEmpty()) {
                    // Играем с самого начала раздачи — с первой главы первой книги.
                    playLocal(firstBook.id, chapters, null)
                    return
                }
            }
            delay(SERVER_PREPARE_POLL_MS)
        }
        Log.d(TAG, "startReleaseDownloadAndWait timeout: books/chapters not appeared")

        // Не дождались — серверная загрузка уже запущена, поллер продолжит
        // свою работу; помечаем раздачу для фоновой докачки.
        _state.update {
            it.copy(
                preparing = false,
                offlineQueued = true,
                title = "Раздача готовится на сервере",
            )
        }
        repository.setReleaseQueued(releaseId, true)
    }

    /** Полноэкранный плеер открыт: поднимаем книгу в быструю очередь. */
    fun onPlayerScreenOpened() {
        val bookId = playerScreenBookId ?: parseMediaId(_state.value.mediaId)?.first
        if (bookId != null) {
            playerScreenBookId = bookId
            downloadQueue.promoteBook(bookId)
        }
    }

    /** Полноэкранный плеер закрыт: недокачанные главы книги — в медленную очередь. */
    fun onPlayerScreenClosed() {
        val bookId = playerScreenBookId
        playerScreenBookId = null
        if (bookId != null) {
            downloadQueue.demoteBook(bookId)
        }
    }

    fun dismissNextVolume() = _requestNextVolume.update { null }

    fun startNextVolume(nextBookId: Int) {
        _requestNextVolume.update { null }
        playBook(nextBookId)
    }

    private fun startPrepare(bookId: Int, chapterOrdinal: Int?) {
        Log.d(TAG, "startPrepare bookId=$bookId chapterOrdinal=$chapterOrdinal")
        saveCurrentProgress()

        prepareJob?.cancel()
        retryJob?.cancel()

        prepareJob = viewModelScope.launch {
            try {
                prepareAndPlay(bookId, chapterOrdinal)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "startPrepare error bookId=$bookId", e)
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
        Log.d(TAG, "prepareAndPlay bookId=$bookId chapterOrdinal=$chapterOrdinal")
        controllerOrNull?.let { c ->
            c.stop()
            c.clearMediaItems()
        }

        _state.update { PlaybackUiState(isServerOnline = it.isServerOnline, preparing = true) }

        // Главы уже в кэше — играем сразу.
        val chapters = repository.getChapters(bookId)
        Log.d(TAG, "prepareAndPlay cached chapters=${chapters.size}")
        if (chapters.isNotEmpty()) {
            return playLocal(bookId, chapters, chapterOrdinal)
        }

        // Глав нет локально.
        val online = app.serverMonitor.availability.value ==
            ru.burkut.audiobooks.data.ServerAvailability.Online
        Log.d(TAG, "prepareAndPlay serverOnline=$online")
        if (online) {
            // Свежая карточка с сервера — после синка должны появиться главы.
            runCatching { repository.getReleaseDetailForBook(bookId, online) }
            val chapters2 = repository.getChapters(bookId)
            Log.d(TAG, "prepareAndPlay chapters after refresh=${chapters2.size}")
            if (chapters2.isNotEmpty()) {
                return playLocal(bookId, chapters2, chapterOrdinal)
            }
            // Сервер доступен, но глав всё ещё нет: книга в статусе
            // «announced» (торрент не качался). Запускаем серверную загрузку
            // и ждём, пока поллер создаст главы.
            startServerDownloadAndWait(bookId, chapterOrdinal)
            return
        }

        // Нет глав локально и нет сервера — ставим раздачу в очередь.
        queueReleaseForBook(bookId)
    }

    /**
     * Книга на сервере в статусе announced — главы ещё не созданы.
     * Запускаем серверную загрузку торрента и опрашиваем каталог,
     * пока не появятся главы, затем начинаем воспроизведение.
     */
    private suspend fun startServerDownloadAndWait(bookId: Int, chapterOrdinal: Int?) {
        Log.d(TAG, "startServerDownloadAndWait bookId=$bookId")
        _state.update {
            it.copy(
                preparing = true,
                title = "Подготовка книги на сервере…",
                artist = null,
                hasMedia = false,
                offlineQueued = false,
            )
        }

        // Сообщаем серверу о начале загрузки (POST /books/{id}/download).
        val started = runCatching {
            Log.d(TAG, "startServerDownload POST /books/$bookId/download")
            repository.startDownload(bookId)
        }
        Log.d(TAG, "startServerDownload result failure=${started.isFailure} err=${started.exceptionOrNull()?.message}")
        if (started.isFailure && repository.getChapters(bookId).isEmpty()) {
            // На случай «already_done» пробуем ещё раз подтянуть карточку.
            runCatching { repository.getReleaseDetailForBook(bookId, online = true) }
            if (repository.getChapters(bookId).isEmpty()) {
                _state.update { it.copy(preparing = false) }
                _error.value = "Не удалось начать загрузку на сервере: " +
                    (started.exceptionOrNull()?.message ?: "неизвестная ошибка")
                return
            }
        }

        // Пока главы не появились — ждём (поллер на сервере создаёт их
        // после появления файлов торрента).
        val deadline = System.currentTimeMillis() + SERVER_PREPARE_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline && currentCoroutineContext().isActive) {
            runCatching { repository.getReleaseDetailForBook(bookId, online = true) }
            val chapters = repository.getChapters(bookId)
            Log.d(TAG, "startServerDownload poll chapters=${chapters.size} elapsed=${System.currentTimeMillis() - (deadline - SERVER_PREPARE_TIMEOUT_MS)}ms")
            if (chapters.isNotEmpty()) {
                playLocal(bookId, chapters, chapterOrdinal)
                return
            }
            // Страховка: если книга-карточка (announced-заглушка) была
            // заменена сервером на другую книгу той же раздачи (например,
            // структура торрента оказалась иной), переключаемся на неё —
            // иначе поллинг «мёртвой» заглушки продолжался бы вечно.
            val adopted = alternativeBookWithChapters(bookId)
            if (adopted != null) {
                Log.d(TAG, "startServerDownload adopt book=${adopted.first} (was bookId=$bookId) chapters=${adopted.second.size}")
                playLocal(adopted.first, adopted.second, chapterOrdinal)
                return
            }
            delay(SERVER_PREPARE_POLL_MS)
        }
        Log.d(TAG, "startServerDownload timeout: chapters not appeared")

        // Не дождались глав — помечаем раздачу для фоновой докачки,
        // серверная загрузка уже запущена и поллер продолжит свою работу.
        _state.update {
            it.copy(
                preparing = false,
                offlineQueued = true,
                title = "Книга готовится на сервере",
            )
        }
        val book = repository.getBookEntity(bookId)
        val releaseId = book?.releaseId
        if (releaseId != null) {
            repository.setReleaseQueued(releaseId, true)
        }
    }

    /** Ищет в той же раздаче книгу (кроме заданной) с уже созданными главами. */
    private suspend fun alternativeBookWithChapters(bookId: Int): Pair<Int, List<ChapterEntity>>? {
        val releaseId = repository.getBookEntity(bookId)?.releaseId ?: return null
        for (b in repository.getBooksByRelease(releaseId)) {
            if (b.id == bookId) continue
            val chapters = repository.getChapters(b.id)
            if (chapters.isNotEmpty()) return b.id to chapters
        }
        return null
    }

    private suspend fun playLocal(bookId: Int, chapters: List<ChapterEntity>, chapterOrdinal: Int?) {
        Log.d(TAG, "playLocal bookId=$bookId chapters=${chapters.size} chapterOrdinal=$chapterOrdinal")

        playerScreenBookId = bookId

        // Определяем стартовую главу заранее (явный ordinal или из сохранённого
        // прогресса) — по ней будем ждать готовности файла перед стартом плеера.
        val startOrdinal = chapterOrdinal ?: run {
            val saved = repository.getProgress(bookId)
            if (saved != null && chapters.any { it.ordinal == saved.chapterOrdinal }) {
                saved.chapterOrdinal
            } else {
                null
            }
        }

        // Книга теперь воспроизводится — её главы переносятся в НАЧАЛО очереди,
        // причём стартовая глава встаёт самой первой: строгая FIFO докачает её
        // полностью раньше остальных, и плеер стартует максимально близко к
        // моменту готовности файла на сервере.
        downloadQueue.promoteBook(bookId, startOrdinal)

        // Докачиваем те главы, которых нет локально, и ждём, пока источник
        // подтвердит готовность (файл стартовой главы появился на диске).
        ensureChaptersDownloaded(bookId, chapters, startOrdinal)

        // Длительности измеряем ПОСЛЕ ожидания готовности — к этому моменту
        // докачанные главы уже на диске, и верхний бар книги сразу показывает
        // реальную сумму, а не 0.
        backfillLocalDurations(chapters)

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
                    val chDuration = (ch.localDurationSec ?: ch.durationSec ?: 0) * 1000L
                    val nearEnd = chDuration > 0 &&
                        saved.positionMs >= chDuration - 2000L
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

    /**
     * Докачивает недостающие главы через очередь (если сервер доступен) и ждёт,
     * пока источник подтвердит готовность: файл главы, с которой начнётся
     * воспроизведение, реально существует и содержит данные (готовый .mp3 или
     * растущий .part). Плеер не начинает работу, пока файл не появился, — это
     * исключает FileNotFoundException (ENOENT) при открытии ещё не созданного .part.
     */
    private suspend fun ensureChaptersDownloaded(
        bookId: Int,
        chapters: List<ChapterEntity>,
        startOrdinal: Int?,
    ) {
        val online = app.serverMonitor.availability.value ==
            ru.burkut.audiobooks.data.ServerAvailability.Online
        if (!online) {
            // Нет доступа: если книга частично есть — играем то, что есть;
            // недостающее поставлено в очередь в DownloadQueue через ServerMonitor.
            return
        }
        val missing = chapters.filterNot { storage.isChapterReady(it) }
        if (missing.isEmpty()) return

        // Стартовая глава — та, что плеер откроет первой.
        val first = chapters.firstOrNull { it.ordinal == startOrdinal }
            ?: missing.minByOrNull { it.ordinal }
            ?: return
        val base = settings.baseUrlOrNull() ?: ""
        fun taskFor(ch: ChapterEntity) = DownloadTask(
            bookId = bookId,
            chapterId = ch.id,
            ordinal = ch.ordinal,
            sizeBytes = ch.sizeBytes,
            url = absoluteUrl(base, "/books/$bookId/stream/${ch.id}"),
        )

        // Стартовую главу выносим в ГОЛОВУ очереди: строгая FIFO докачает её
        // полностью раньше остальных — плеер не ждёт «своей очереди» среди
        // других глав. Остальные главы книги встают за ней по возрастанию
        // ordinal: пока голова не докачана, запросы по ним не отправляются.
        downloadQueue.awaitChapter(taskFor(first))
        for (ch in missing) {
            if (ch.id == first.id) continue
            downloadQueue.enqueue(taskFor(ch))
        }
        Log.d(TAG, "ensureChaptersDownloaded waiting for file book=$bookId ordinal=${first.ordinal}")
        val deadline = System.currentTimeMillis() + CHAPTER_READY_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline && currentCoroutineContext().isActive) {
            val f = storage.playbackFile(bookId, first.ordinal)
            if (f.exists() && f.length() > 0) {
                Log.d(TAG, "ensureChaptersDownloaded file ready: ${f.path} size=${f.length()}")
                return
            }
            delay(CHAPTER_READY_POLL_MS)
        }
        Log.d(TAG, "ensureChaptersDownloaded timeout waiting for ordinal=${first.ordinal}")
        // Файл так и не появился — очередь докачает его в фоне, а плеер
        // стартует позже через локальный запрос (GrowingFileDataSource сам
        // дождётся файла, если тот появится к моменту открытия).
    }

    /** Книга недоступна оффлайн — ставим раздачу в фоновую очередь. */
    private suspend fun queueReleaseForBook(bookId: Int) {
        Log.d(TAG, "queueReleaseForBook bookId=$bookId")
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
        private const val TAG = "PlayerVM"
        fun chapterMediaId(bookId: Int, chapterOrdinal: Int): String = "$bookId:$chapterOrdinal"
        private const val SAVE_PROGRESS_INTERVAL_MS = 5_000L
        /** Максимальное время ожидания появления глав после старта серверной загрузки. */
        private const val SERVER_PREPARE_TIMEOUT_MS = 120_000L
        /** Период опроса каталога, пока главы создаются на сервере. */
        private const val SERVER_PREPARE_POLL_MS = 2_000L
        /** Максимальное время ожидания появления файла главы перед стартом плеера.
         *  Держим коротким: стартовая глава качается первой (сервер отключил
         *  sequential), а если файл задержится — плеер стартует на растущем .part,
         *  который сам дождётся появления данных. */
        private const val CHAPTER_READY_TIMEOUT_MS = 20_000L
        /** Период опроса готовности файла главы. */
        private const val CHAPTER_READY_POLL_MS = 250L
    }
}

fun absoluteUrl(baseUrl: String, url: String): String =
    if (url.startsWith("http://") || url.startsWith("https://")) {
        url
    } else {
        baseUrl.trimEnd('/') + if (url.startsWith("/")) url else "/$url"
    }
