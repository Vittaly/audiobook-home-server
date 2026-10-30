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

/**
 * Состояние запроса воспроизведения книги (гейт готовности).
 *
 * Когда пользователь нажимает «Прослушать» на карточке книги, ViewModel
 * начинает подготовку (загрузку на сервере / ожидание глав). Пока главы
 * не готовы — плеер не открывается; вместо этого [MainActivity] показывает
 * оверлей поверх карточки книги с индикацией статуса.
 *
 * Если за [PLAY_REQUEST_TIMEOUT_MS] главы не появились — оверлей гасится,
 * пользователь остаётся на карточке книги (загрузка продолжается в фоне).
 */
data class PlayRequestUiState(
    val requestedBookHash: String? = null,
    val waiting: Boolean = false,
    val status: String? = null,
)

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
    val mediaId: String? = null,        // "bookHash:ordinal"
    val preparing: Boolean = false,
    val offlineQueued: Boolean = false, // книга в очереди (нет доступа к серверу)
)

class PlayerViewModel(application: Application) : ViewModel() {

    private val context = application.applicationContext
    private val app = application as App
    private val repository = app.repository
    private val settings = app.settings
    private val storage = app.storage
    private val coverStore = app.coverStore
    private val downloadQueue = app.downloadQueue

    private val _state = MutableStateFlow(PlaybackUiState())
    val state: StateFlow<PlaybackUiState> = _state.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private val _requestNextVolume = MutableStateFlow<BookEntity?>(null)
    val requestNextVolume: StateFlow<BookEntity?> = _requestNextVolume.asStateFlow()

    /** Гейт готовности: пока главы не готовы — плеер не открывается. */
    private val _playRequest = MutableStateFlow(PlayRequestUiState())
    val playRequest: StateFlow<PlayRequestUiState> = _playRequest.asStateFlow()

    private var prepareJob: Job? = null
    private var retryJob: Job? = null

    /** Схлопывание частых пересчётов прогресса книги в один фоновый прогон. */
    private var bookProgressJob: Job? = null
    private var bookProgressPending = false

    /** Книга, открытая в полноэкранном плеере (для promote/demote очереди). */
    private var playerScreenBookHash: String? = null

    /**
     * Текущий URL обложки для UI. Хранится отдельно от mediaMetadata, потому
     * что финальная обложка появляется уже после старта воспроизведения:
     * [coverPollJob] опрашивает сервер и обновляет URL (с cache-busting nonce),
     * а навигация по медиа не пересоздаётся — поэтому snapshot() берёт artUri
     * именно отсюда, а не из mediaMetadata.
     */
    private var currentCoverUrl: String? = null

    /**
     * Опрос готовности финальной обложки (HEAD /books/{hash}/cover). Пока сервер
     * отдаёт признак X-Cover-Temp, обновляем URL с nonce, чтобы Coil перечитал
     * картинку; как только признак снят — фиксируем финал и выходим.
     */
    private var coverPollJob: Job? = null

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
        val (bookHash, chapterOrdinal) = parsed
        val position = controller.currentPosition
        if (position > 0) {
            viewModelScope.launch {
                runCatching { repository.saveProgress(bookHash, chapterOrdinal, position) }
            }
        }
    }

    private fun parseMediaId(mediaId: String?): Pair<String, Int>? {
        if (mediaId == null) return null
        val parts = mediaId.split(":")
        if (parts.size != 2) return null
        val bookHash = parts[0]
        val chapterOrdinal = parts[1].toIntOrNull() ?: return null
        return bookHash to chapterOrdinal
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
                    artUri = currentCoverUrl ?: prev.artUri,
                )
            } else {
                prev.copy(
                    hasMedia = controller.mediaItemCount > 0,
                    isPlaying = controller.playWhenReady && controller.isPlaying,
                    isBuffering = controller.playbackState == Player.STATE_BUFFERING,
                    title = item?.mediaMetadata?.artist?.toString(),      // книга
                    artist = item?.mediaMetadata?.title?.toString(),      // глава
                    artUri = currentCoverUrl ?: item?.mediaMetadata?.artworkUri?.toString(),
                    positionMs = controller.currentPosition,
                    durationMs = if (duration > 0) {
                        duration
                    } else if (item?.mediaId == prev.mediaId) {
                        prev.durationMs
                    } else {
                        0L
                    },
                    bufferedMs = controller.bufferedPosition,
                    mediaId = item?.mediaId,
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
        val (bookHash, currentOrdinal) = parsed
        val chapters = repository.getChapters(bookHash)
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

    /**
     * Запрос воспроизведения книги по её хэшу.
     *
     * Если главы уже готовы — плеер стартует немедленно. Если нет —
     * ViewModel запускает подготовку (серверная загрузка / ожидание глав)
     * и через гейт [playRequest] сообщает UI о статусе ожидания.
     * [MainActivity] подписан на [playRequest] и показывает оверлей
     * поверх карточки книги, пока главы не появились.
     *
     * Если за [PLAY_REQUEST_TIMEOUT_MS] главы не готовы — оверлей гасится,
     * пользователь остаётся на карточке (загрузка продолжается в фоне).
     */
    fun playBook(bookHash: String) = startPrepare(bookHash, null)

    fun playChapter(bookHash: String, chapterOrdinal: Int) = startPrepare(bookHash, chapterOrdinal)

    /**
     * Отмена запроса воспроизведения (пользователь закрыл оверлей / таймаут).
     * Подготовка книги при этом НЕ отменяется — загрузка продолжается в фоне.
     */
    fun cancelPlayRequest() {
        _playRequest.update { PlayRequestUiState() }
    }

    /** Полноэкранный плеер открыт: поднимаем книгу в быструю очередь. */
    fun onPlayerScreenOpened() {
        val bookHash = playerScreenBookHash ?: parseMediaId(_state.value.mediaId)?.first
        if (bookHash != null) {
            playerScreenBookHash = bookHash
            downloadQueue.promoteBook(bookHash)
        }
    }

    /** Полноэкранный плеер закрыт: недокачанные главы книги — в медленную очередь. */
    fun onPlayerScreenClosed() {
        val bookHash = playerScreenBookHash
        playerScreenBookHash = null
        // Плеер закрыт — зачем продолжать опрашивать обложку в фоне.
        coverPollJob?.cancel()
        if (bookHash != null) {
            downloadQueue.demoteBook(bookHash)
        }
    }

    fun dismissNextVolume() = _requestNextVolume.update { null }

    fun startNextVolume(nextBookHash: String) {
        _requestNextVolume.update { null }
        playBook(nextBookHash)
    }

    private fun startPrepare(bookHash: String, chapterOrdinal: Int?) {
        Log.d(TAG, "startPrepare bookHash=$bookHash chapterOrdinal=$chapterOrdinal")
        saveCurrentProgress()

        prepareJob?.cancel()
        retryJob?.cancel()
        // Новая книга: сбрасываем обложку прежней книги и её опрос.
        resetCurrentCover()

        prepareJob = viewModelScope.launch {
            try {
                prepareAndPlay(bookHash, chapterOrdinal)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                Log.e(TAG, "startPrepare error bookHash=$bookHash", e)
                _state.update { s -> s.copy(preparing = false) }
                _error.value = e.message ?: "Ошибка воспроизведения"
            } finally {
                // Какой бы ни была ошибка — гасим оверлей ожидания.
                _playRequest.update { PlayRequestUiState() }
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
            val (bookHash, chapterOrdinal) = parsed
            runBlocking {
                runCatching { repository.saveProgress(bookHash, chapterOrdinal, position) }
            }
        }

        prepareJob?.cancel()
        retryJob?.cancel()
        coverPollJob?.cancel()

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

    private suspend fun prepareAndPlay(bookHash: String, chapterOrdinal: Int?) {
        Log.d(TAG, "prepareAndPlay bookHash=$bookHash chapterOrdinal=$chapterOrdinal")
        controllerOrNull?.let { c ->
            c.stop()
            c.clearMediaItems()
        }

        _state.update { PlaybackUiState(isServerOnline = it.isServerOnline, preparing = true) }

        // Главы уже в кэше — играем сразу.
        val chapters = repository.getChapters(bookHash)
        Log.d(TAG, "prepareAndPlay cached chapters=${chapters.size}")
        if (chapters.isNotEmpty()) {
            return playLocal(bookHash, chapters, chapterOrdinal)
        }

        // Глав нет локально.
        val online = app.serverMonitor.availability.value ==
            ru.burkut.audiobooks.data.ServerAvailability.Online
        Log.d(TAG, "prepareAndPlay serverOnline=$online")
        if (online) {
            // Свежая карточка с сервера — после синка должны появиться главы.
            runCatching { repository.getBookDetail(bookHash, online) }
            val chapters2 = repository.getChapters(bookHash)
            Log.d(TAG, "prepareAndPlay chapters after refresh=${chapters2.size}")
            if (chapters2.isNotEmpty()) {
                return playLocal(bookHash, chapters2, chapterOrdinal)
            }
            // Сервер доступен, но глав всё ещё нет: книга в статусе
            // «announced» (торрент не качался). Запускаем серверную загрузку
            // и ждём, пока поллер создаст главы.
            startServerDownloadAndWait(bookHash, chapterOrdinal)
            return
        }

        // Нет глав локально и нет сервера — ставим книгу в очередь.
        queueOffline(bookHash)
    }

    /**
     * Книга на сервере в статусе announced — главы ещё не созданы.
     * Запускаем серверную загрузку торрента и опрашиваем каталог,
     * пока не появятся главы, затем начинаем воспроизведение.
     */
    private suspend fun startServerDownloadAndWait(bookHash: String, chapterOrdinal: Int?) {
        Log.d(TAG, "startServerDownloadAndWait bookHash=$bookHash")

        // Устанавливаем гейт готовности: UI покажет оверлей на карточке книги.
        val deadline = System.currentTimeMillis() + PLAY_REQUEST_TIMEOUT_MS
        _playRequest.update {
            PlayRequestUiState(
                requestedBookHash = bookHash,
                waiting = true,
                status = "Подготовка книги на сервере…",
            )
        }

        _state.update {
            it.copy(
                preparing = true,
                title = "Подготовка книги на сервере…",
                artist = null,
                hasMedia = false,
                offlineQueued = false,
            )
        }
        // Пока ждём главы — показываем обложку книги, как только она известна.
        applyBookCover(bookHash)

        // Сообщаем серверу о начале загрузки (POST /books/{hash}/download).
        val started = runCatching {
            Log.d(TAG, "startServerDownload POST /books/$bookHash/download")
            repository.startDownload(bookHash)
        }
        Log.d(TAG, "startServerDownload result failure=${started.isFailure} err=${started.exceptionOrNull()?.message}")
        if (started.isFailure && repository.getChapters(bookHash).isEmpty()) {
            // На случай «already_done» пробуем ещё раз подтянуть карточку.
            runCatching { repository.getBookDetail(bookHash, online = true) }
            if (repository.getChapters(bookHash).isEmpty()) {
                _state.update { it.copy(preparing = false) }
                _playRequest.update {
                    PlayRequestUiState(
                        requestedBookHash = bookHash,
                        waiting = false,
                        status = "Не удалось начать загрузку на сервере",
                    )
                }
                _error.value = "Не удалось начать загрузку на сервере: " +
                    (started.exceptionOrNull()?.message ?: "неизвестная ошибка")
                return
            }
        }

        // Пока главы не появились — ждём (поллер на сервере создаёт их
        // после появления файлов торрента).
        val serverDeadline = System.currentTimeMillis() + SERVER_PREPARE_TIMEOUT_MS
        while (System.currentTimeMillis() < serverDeadline && currentCoroutineContext().isActive) {
            runCatching { repository.getBookDetail(bookHash, online = true) }
            val chapters = repository.getChapters(bookHash)
            Log.d(TAG, "startServerDownload poll chapters=${chapters.size} elapsed=${System.currentTimeMillis() - (deadline - PLAY_REQUEST_TIMEOUT_MS)}ms")
            if (chapters.isNotEmpty()) {
                // Главы появились — гасим оверлей и стартуем плеер.
                _playRequest.update { PlayRequestUiState() }
                playLocal(bookHash, chapters, chapterOrdinal)
                return
            }
            // Главы ещё не созданы — держим обложку актуальной по мере синка.
            applyBookCover(bookHash)

            // Если истёк таймаут показа оверлея (30 с) — гасим его,
            // но продолжаем ждать главы (сам процесс подготовки не отменяем).
            if (_playRequest.value.waiting && System.currentTimeMillis() >= deadline) {
                Log.d(TAG, "startServerDownload play request timeout (${PLAY_REQUEST_TIMEOUT_MS}ms) — hiding overlay, continuing download")
                _playRequest.update { PlayRequestUiState() }
            }

            delay(SERVER_PREPARE_POLL_MS)
        }
        Log.d(TAG, "startServerDownload timeout: chapters not appeared")

        // Не дождались глав — серверная загрузка уже запущена, поллер продолжит
        // свою работу.
        _state.update {
            it.copy(
                preparing = false,
                offlineQueued = true,
                title = "Книга готовится на сервере",
            )
        }
    }

    private suspend fun playLocal(bookHash: String, chapters: List<ChapterEntity>, chapterOrdinal: Int?) {
        Log.d(TAG, "playLocal bookHash=$bookHash chapters=${chapters.size} chapterOrdinal=$chapterOrdinal")

        playerScreenBookHash = bookHash

        val online = app.serverMonitor.availability.value ==
            ru.burkut.audiobooks.data.ServerAvailability.Online

        // Состав плейлиста определяет источник (BookStorage): онлайн — все главы
        // (недостающие докачает очередь, плеер стартует на растущем .part),
        // оффлайн — только реально присутствующие файлы. Плеер просто играет то,
        // что ему дали, и не занимается проверкой кэша.
        val playlist = storage.playableChapters(chapters, online)
        Log.d(TAG, "playLocal online=$online playable=${playlist.size}/${chapters.size}")

        // Нечего играть локально и дотянуть нечем — книга уходит в очередь.
        if (playlist.isEmpty()) {
            _state.update { it.copy(preparing = false, isBuffering = false) }
            queueOffline(bookHash)
            return
        }

        // Стартовая глава: явный ordinal либо из сохранённого прогресса.
        // Всегда должна присутствовать в плейлисте — иначе начинаем с первой
        // доступной (в оффлайне пропущенные главы не должны блокировать старт).
        val requestedOrdinal = chapterOrdinal ?: run {
            val saved = repository.getProgress(bookHash)
            if (saved != null && playlist.any { it.ordinal == saved.chapterOrdinal }) {
                saved.chapterOrdinal
            } else {
                null
            }
        }
        val startOrdinal = requestedOrdinal?.takeIf { o -> playlist.any { it.ordinal == o } }
            ?: playlist.first().ordinal

        // Книга теперь воспроизводится — её главы переносятся в НАЧАЛО очереди,
        // причём стартовая глава встаёт самой первой: строгая FIFO докачает её
        // полностью раньше остальных, и плеер стартует максимально близко к
        // моменту готовности файла на сервере.
        downloadQueue.promoteBook(bookHash, startOrdinal)

        // Докачиваем те главы, которых нет локально (только онлайн), и ждём,
        // пока источник подтвердит готовность файла стартовой главы.
        ensureChaptersDownloaded(bookHash, chapters, startOrdinal)

        val book = repository.getBookEntity(bookHash)
        val coverUri = book?.coverUrl?.let { Uri.parse(it) }

        // Оффлайн обложку берём из локального кэша (файл рядом с аудио) —
        // сервер недоступен, тянуть картинку неоткуда. Онлайн показываем
        // серверную обложку сразу, а финальную подхватываем опросом заголовков.
        val localCover = coverStore.cachedBookCover(bookHash)
        if (!online && localCover != null) {
            currentCoverUrl = Uri.fromFile(localCover).toString()
        } else {
            currentCoverUrl = book?.coverUrl
        }
        if (online && book?.coverIsTemp == true) {
            startCoverPolling(bookHash, book.coverUrl)
        } else {
            coverPollJob?.cancel()
        }

        // Для растущих .part-файлов сообщаем фабрике целевой размер главы —
        // GrowingFileDataSource должен знать, когда глава закончится.
        for (ch in playlist) {
            app.localDataSourceFactory.setTargetLength(bookHash, ch.ordinal, ch.sizeBytes)
        }

        val items = playlist.map { ch ->
            MediaItem.Builder()
                .setMediaId(chapterMediaId(bookHash, ch.ordinal))
                .setUri(Uri.fromFile(storage.playbackFile(bookHash, ch.ordinal)))
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
            index = playlist.indexOfFirst { it.ordinal == chapterOrdinal }.coerceAtLeast(0)
        } else {
            val saved = repository.getProgress(bookHash)
            if (saved != null) {
                val idx = playlist.indexOfFirst { it.ordinal == saved.chapterOrdinal }
                if (idx >= 0) {
                    val ch = playlist[idx]
                    val chDuration = (ch.localDurationSec ?: ch.durationSec ?: 0) * 1000L
                    val nearEnd = chDuration > 0 &&
                        saved.positionMs >= chDuration - 2000L
                    when {
                        nearEnd && idx + 1 < playlist.size -> { index = idx + 1; position = 0L }
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
        // Снимаем «колесо» сразу после старта: измерение длительностей глав —
        // независимая операция, она не должна задерживать начало воспроизведения
        // (особенно в оффлайне, когда книга уже полностью скачана).
        _state.update { it.copy(preparing = false, isBuffering = false) }
        updateBookProgress()
    }

    /**
     * Опрашивает готовность финальной обложки, пока сервер отдаёт временную
     * (заголовок X-Cover-Temp). Каждую итерацию запрашивает только заголовки
     * (HEAD /books/{hash}/cover), а не карточку книги и не саму картинку. Пока
     * обложка временная — обновляет [currentCoverUrl] тем же URL с cache-busting
     * nonce (Coil перечитывает картинку), как только признак снят — фиксирует
     * финальный URL и выходит. Ошибки сети не фатальны: просто повторим позже.
     */
    private fun startCoverPolling(bookHash: String, initialCoverUrl: String?) {
        coverPollJob?.cancel()
        if (initialCoverUrl == null) return
        coverPollJob = viewModelScope.launch {
            var attempt = 0
            while (isActive && attempt < COVER_POLL_MAX_ATTEMPTS) {
                val isTemp = repository.coverIsTemp(bookHash)
                if (isTemp == false) {
                    // Финал: сохраняем обложку в локальный файл и показываем
                    // её — дальше она доступна оффлайн без сервера.
                    val local = coverStore.ensureBookCover(bookHash, force = true)
                    currentCoverUrl = local?.let { Uri.fromFile(it).toString() }
                        ?: coverUrlWithNonce(initialCoverUrl)
                    return@launch
                }
                if (isTemp == true) {
                    currentCoverUrl = coverUrlWithNonce(initialCoverUrl)
                }
                attempt++
                delay(COVER_POLL_INTERVAL_MS)
            }
        }
    }

    /** Дописывает cache-busting nonce, чтобы Coil перечитал обложку. */
    private fun coverUrlWithNonce(url: String): String {
        val sep = if (url.contains('?')) '&' else '?'
        return "$url${sep}v=${System.currentTimeMillis()}"
    }

    /**
     * Пока книга готовится (глав ещё нет), держим в UI обложку этой книги, как
     * только сервер её отдал: не перетираем уже показанную обложку и запускаем
     * опрос финальной версии, если текущая — временная.
     */
    private suspend fun applyBookCover(bookHash: String) {
        val b = repository.getBookEntity(bookHash) ?: return
        val url = b.coverUrl ?: return
        if (currentCoverUrl.isNullOrEmpty()) {
            currentCoverUrl = url
            _state.update { it.copy(artUri = url) }
        }
        if (b.coverIsTemp) {
            if (coverPollJob?.isActive != true) startCoverPolling(bookHash, url)
        } else {
            coverPollJob?.cancel()
        }
    }

    /**
     * Сброс обложки при старте новой книги: останавливаем прежний опрос,
     * очищаем URL, а также синхронно чистим медиаэлементы контроллера. Иначе во
     * время ожидания (спиннер) в плеере продолжала бы показываться обложка
     * предыдущей книги: либо из currentCoverUrl, либо из artworkUri ещё не
     * выгруженного старого трека. Обложка новой книги появится в playLocal().
     */
    private fun resetCurrentCover() {
        coverPollJob?.cancel()
        coverPollJob = null
        currentCoverUrl = null
        controllerOrNull?.let { c ->
            c.stop()
            c.clearMediaItems()
        }
        _state.update { it.copy(artUri = null) }
    }

    /**
     * Докачивает недостающие главы через очередь (если сервер доступен) и ждёт,
     * пока источник подтвердит готовность: файл главы, с которой начнётся
     * воспроизведение, реально существует и содержит данные (готовый .mp3 или
     * растущий .part). Плеер не начинает работу, пока файл не появился, — это
     * исключает FileNotFoundException (ENOENT) при открытии ещё не созданного .part.
     */
    private suspend fun ensureChaptersDownloaded(
        bookHash: String,
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
            bookHash = bookHash,
            chapterId = ch.id,
            ordinal = ch.ordinal,
            sizeBytes = ch.sizeBytes,
            url = absoluteUrl(base, "/books/$bookHash/stream/${ch.id}"),
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
        Log.d(TAG, "ensureChaptersDownloaded waiting for file book=$bookHash ordinal=${first.ordinal}")
        val deadline = System.currentTimeMillis() + CHAPTER_READY_TIMEOUT_MS
        while (System.currentTimeMillis() < deadline && currentCoroutineContext().isActive) {
            val f = storage.playbackFile(bookHash, first.ordinal)
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

    /** Книга недоступна — ставим в фоновую очередь. */
    private suspend fun queueOffline(bookHash: String) {
        Log.d(TAG, "queueOffline bookHash=$bookHash")
        _state.update {
            it.copy(preparing = false, offlineQueued = true, title = "Книга в очереди")
        }
        // DownloadQueue подхватит книгу, когда ServerMonitor сообщит online.
    }

    /** После окончания последней главы книги — предложить следующий том. */
    private fun maybeRequestNextVolume() {
        viewModelScope.launch {
            val mediaId = controllerOrNull?.currentMediaItem?.mediaId ?: return@launch
            val parsed = parseMediaId(mediaId) ?: return@launch
            val (bookHash, _) = parsed
            val book = repository.getBookEntity(bookHash) ?: return@launch
            val siblings = repository.getBooksByRelease(book.releaseId, bookHash)
            // Следующим будет том со следующим порядковым номером.
            val next = siblings.firstOrNull { it.ordinal > book.ordinal }
                ?: siblings.firstOrNull()
                ?: return@launch
            _requestNextVolume.value = next
        }
    }

    companion object {
        private const val TAG = "PlayerVM"
        fun chapterMediaId(bookHash: String, chapterOrdinal: Int): String = "$bookHash:$chapterOrdinal"
        private const val SAVE_PROGRESS_INTERVAL_MS = 5_000L
        /** Максимальное время ожидания появления глав после старта серверной загрузки. */
        private const val SERVER_PREPARE_TIMEOUT_MS = 120_000L
        /** Период опроса каталога, пока главы создаются на сервере. */
        private const val SERVER_PREPARE_POLL_MS = 2_000L
        /** Максимальное время ожидания появления файла главы перед стартом плеера. */
        private const val CHAPTER_READY_TIMEOUT_MS = 20_000L
        /** Период опроса готовности файла главы. */
        private const val CHAPTER_READY_POLL_MS = 250L
        /** Период опроса готовности финальной обложки. */
        private const val COVER_POLL_INTERVAL_MS = 5_000L
        /** Предохранитель: максимум итераций опроса обложки (≈10 минут). */
        private const val COVER_POLL_MAX_ATTEMPTS = 120
        /**
         * Таймаут показа оверлея ожидания на карточке книги (30 секунд).
         * Если за это время главы не появились — оверлей гасится, пользователь
         * остаётся на карточке, а загрузка продолжается в фоне.
         */
        private const val PLAY_REQUEST_TIMEOUT_MS = 30_000L
    }
}

fun absoluteUrl(baseUrl: String, url: String): String =
    if (url.startsWith("http://") || url.startsWith("https://")) {
        url
    } else {
        baseUrl.trimEnd('/') + if (url.startsWith("/")) url else "/$url"
    }
