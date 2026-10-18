package ru.burkut.audiobooks.player

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import ru.burkut.audiobooks.data.BookRepository
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit

/** Одна глава книги, готовая к постановке в очередь (chapterId, ordinal, размер, url). */
data class ChapterDownloadSpec(
    val chapterId: Int,
    val ordinal: Int,
    val sizeBytes: Long,
    val url: String,
)

/** Одна задача в очереди — глава, которую надо скачать. */
data class DownloadTask(
    val bookId: Int,
    val chapterId: Int,
    val ordinal: Int,
    val sizeBytes: Long,
    val url: String,          // абсолютный stream_url
)

/** Состояние очереди загрузки. */
data class DownloadQueueState(
    val isActive: Boolean = false,
    val currentBookId: Int? = null,
    val currentChapterOrdinal: Int? = null,
    val downloadedBytes: Long = 0,
    val totalBytes: Long = 0,
    val tasksPending: Int = 0,
)

/**
 * Очередь загрузки глав на устройство. Две независимые очереди, один воркер:
 *
 * 1. БЫСТРАЯ ([pendingHigh]) — главы книги, открытой сейчас в плеере. Работает
 *    строгой FIFO: пока глава в голове не докачана ПОЛНОСТЬЮ (или не вынесена
 *    перестановкой), следующие не начинаются и запросы по ним не отправляются.
 *    При 409 голова повторяется на месте с короткой задержкой (2с…10с), а не
 *    уходит в хвост — так стартовая глава докачивается максимально близко к
 *    моменту её готовности на сервере, без 409-шторма. Пока в быстрой очереди
 *    есть голова, воркер НЕ трогает фоновую очередь.
 *
 * 2. ФОНОВАЯ ([pending]) — книги, которые не удалось скачать сразу и ушедшие
 *    в фон. Работает КАРУСЕЛЬЮ (round-robin), как раньше: берём задачу из
 *    головы, если сервер вернул 409 — «усыпляем» её с экспоненциальным
 *    бэкоффом (10с…вплоть до суток) и переставляем в ХВОСТ, затем пробуем
 *    следующую. Карусель не мешает быстрой очереди (быстрая всегда впереди).
 *
 * Общее: пишется в `{ordinal}.mp3.part`, по завершении переименовывается в
 * `{ordinal}.mp3`; статусы и байты обновляются в Room; пауза/возобновление —
 * через [setServerAvailable].
 */
class DownloadQueue(
    private val repository: BookRepository,
    private val storage: BookStorage,
) {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val httpClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    /** Фоновая (медленная) очередь — книги в фоне, режим карусели. */
    @Volatile
    private var pending = ConcurrentLinkedQueue<DownloadTask>()

    /** Быстрая очередь — главы книги, открытой в плеере: строгая FIFO. */
    @Volatile
    private var pendingHigh = ConcurrentLinkedQueue<DownloadTask>()

    /** Задача учтена в очереди или качается прямо сейчас (идемпотентность). */
    private val activeTasks = ConcurrentHashMap<Int, Boolean>()

    /** Когда главу можно снова попробовать скачать (chapterId -> ms epoch). */
    private val snoozedUntil = ConcurrentHashMap<Int, Long>()

    /** Сколько 409/сбоев подряд получила глава (chapterId -> кол-во попыток). */
    private val notReadyAttempts = ConcurrentHashMap<Int, Int>()

    /** Книга, открытая сейчас в полноэкранном плеере (null — плеер закрыт). */
    @Volatile
    private var playerBookId: Int? = null

    /** Защита пересборки очередей (воркер не должен видеть «дыры»). */
    private val queueLock = Any()

    private val _state = MutableStateFlow(DownloadQueueState())
    val state: StateFlow<DownloadQueueState> = _state.asStateFlow()

    @Volatile
    private var serverAvailable = false
    private var worker: Job? = null

    private val _onBookReady = MutableStateFlow<Pair<Int, String>?>(null)
    val onBookReady: StateFlow<Pair<Int, String>?> = _onBookReady.asStateFlow()

    /** Сервер доступен/недоступен — запускаем/приостанавливаем очередь. */
    fun setServerAvailable(available: Boolean) {
        serverAvailable = available
        if (available) ensureWorker()
    }

    private fun ensureWorker() {
        if (worker?.isActive == true) return
        worker = scope.launch {
            // --- Состояние карусели фоновой очереди ---
            var sleepingMisses = 0
            var nextWake = 0L

            while (true) {
                if (!serverAvailable) {
                    delay(500)
                    sleepingMisses = 0
                    nextWake = 0L
                    continue
                }

                // ============ 1. БЫСТРАЯ ОЧЕРЕДЬ: строгая FIFO ============
                val fast = synchronized(queueLock) { pendingHigh.firstOrNull() }
                if (fast != null) {
                    when (downloadChapter(fast)) {
                        ChapterDownloadOutcome.COMPLETE -> {
                            // Голова докачана ПОЛНОСТЬЮ — только теперь снимаем
                            // её и переходим к следующей.
                            synchronized(queueLock) {
                                pendingHigh.removeAll { it.chapterId == fast.chapterId }
                            }
                            activeTasks.remove(fast.chapterId)
                            notReadyAttempts.remove(fast.chapterId)
                            snoozedUntil.remove(fast.chapterId)
                            _state.value = _state.value.copy(tasksPending = pendingSize())
                            markBookIfReady(fast.bookId)
                        }

                        ChapterDownloadOutcome.NOT_READY,
                        ChapterDownloadOutcome.RETRY,
                        -> {
                            // Голова НЕ переставляется в хвост: строгая FIFO.
                            // Задержка ФИКСИРОВАННАЯ (2с), без роста — быстрая
                            // очередь не должна замедляться: повторяем голову
                            // каждые 2с, пока она не скачается, ровно до тех
                            // пор, пока плеер ждёт книгу (общий таймаут ожидания
                            // — на клиенте, в PlayerViewModel). Как только книга
                            // уйдёт в фон (demoteBook), глава попадёт в карусель
                            // фоновой очереди с её собственным бэкоффом.
                            Log.d(TAG, "fast retry head book=${fast.bookId} " +
                                "ord=${fast.ordinal} retryIn=${FAST_RETRY_DELAY_MS}ms")
                            delay(FAST_RETRY_DELAY_MS)
                        }
                    }
                    continue
                }

                // ============ 2. ФОНОВАЯ ОЧЕРЕДЬ: карусель ============
                val task = synchronized(queueLock) { pending.poll() }
                if (task == null) {
                    _state.value = _state.value.copy(isActive = false)
                    sleepingMisses = 0
                    nextWake = 0L
                    delay(1000)
                    continue
                }

                // Задача «спит» до своего дедлайна (после часа неудач — раз в
                // час). Проскакиваем её БЕЗ задержки и продолжаем карусель —
                // спящие задачи не должны блокировать очередь паузами.
                val due = snoozedUntil[task.chapterId] ?: 0L
                if (due > System.currentTimeMillis()) {
                    synchronized(queueLock) { pending.add(task) }
                    if (nextWake == 0L || due < nextWake) nextWake = due
                    sleepingMisses++
                    if (sleepingMisses >= 64) {
                        // Долго попадаются только спящие — вся очередь «спит».
                        val wait = (nextWake - System.currentTimeMillis())
                            .coerceAtLeast(100)
                        sleepingMisses = 0
                        nextWake = 0L
                        delay(wait)
                    }
                    continue
                }
                sleepingMisses = 0
                nextWake = 0L

                when (downloadChapter(task)) {
                    ChapterDownloadOutcome.COMPLETE -> {
                        activeTasks.remove(task.chapterId)
                        notReadyAttempts.remove(task.chapterId)
                        snoozedUntil.remove(task.chapterId)
                        _state.value = _state.value.copy(tasksPending = pendingSize())
                        markBookIfReady(task.bookId)
                    }

                    ChapterDownloadOutcome.NOT_READY -> {
                        // Карусель: «усыпляем» задачу с экспоненциальным
                        // бэкоффом (10с, 20с, 40с … до суток) и уводим в хвост,
                        // чтобы попробовать следующую главу другой книги.
                        val attempts = (notReadyAttempts[task.chapterId] ?: 0) + 1
                        notReadyAttempts[task.chapterId] = attempts
                        val shift = (attempts - 1).coerceIn(0, 20)
                        val backoffMs = minOf(
                            NOT_READY_DELAY_MS shl shift,
                            MAX_RETRY_DELAY_MS,
                        )
                        snoozedUntil[task.chapterId] =
                            System.currentTimeMillis() + backoffMs
                        Log.d(TAG, "bg not-ready book=${task.bookId} " +
                            "ord=${task.ordinal} attempts=$attempts " +
                            "retryIn=${backoffMs / 60000}min")
                        synchronized(queueLock) { pending.add(task) }
                        _state.value = _state.value.copy(tasksPending = pendingSize())
                        delay(SNOOZE_WAKE_MS)
                    }

                    ChapterDownloadOutcome.RETRY -> {
                        // Временный сетевой сбой — короткий ретрай, в хвост.
                        synchronized(queueLock) { pending.add(task) }
                        _state.value = _state.value.copy(tasksPending = pendingSize())
                        delay(RETRY_DELAY_MS)
                    }
                }
            }
        }
    }

    /** Глава нужна текущему воспроизведению? — книга открыта сейчас в плеере. */
    private fun isPriority(bookId: Int): Boolean = playerBookId == bookId

    /** Добавить главу в очередь (идемпотентно по chapterId). */
    fun enqueue(task: DownloadTask) {
        if (activeTasks.putIfAbsent(task.chapterId, true) != null) return
        // Свежая постановка = новая попытка: сбрасываем «спящий» режим.
        notReadyAttempts.remove(task.chapterId)
        snoozedUntil.remove(task.chapterId)
        val queue = if (isPriority(task.bookId)) pendingHigh else pending
        synchronized(queueLock) { queue.add(task) }
        _state.value = _state.value.copy(tasksPending = pendingSize())
        if (serverAvailable) ensureWorker()
    }

    /**
     * Вынести главу в ГОЛОВУ быстрой очереди: её ждёт плеер (стартовая глава).
     * Если задача стояла в любой из очередей — переносим в начало быстрой.
     */
    fun awaitChapter(task: DownloadTask) {
        activeTasks.putIfAbsent(task.chapterId, true)
        notReadyAttempts.remove(task.chapterId)
        snoozedUntil.remove(task.chapterId)
        synchronized(queueLock) {
            pendingHigh.removeAll { it.chapterId == task.chapterId }
            pending.removeAll { it.chapterId == task.chapterId }
            pendingHigh.add(task)
        }
        // add() в CLQ кладёт в хвост; пересоберём так, чтобы задача оказалась
        // первой — переставляем все главы этой книги в начало (стартовая
        // первой), как в promoteBook.
        promoteBook(task.bookId, task.ordinal)
        _state.value = _state.value.copy(tasksPending = pendingSize())
        if (serverAvailable) ensureWorker()
    }

    /**
     * Вернулись в плеер с книгой [bookId] — её недокачанные главы переносятся в
     * БЫСТРУЮ очередь. Стартовая глава [startOrdinal] ставится самой первой,
     * остальные — по возрастанию ordinal.
     */
    fun promoteBook(bookId: Int, startOrdinal: Int? = null) {
        playerBookId = bookId
        var moved = 0
        synchronized(queueLock) {
            val mine = pendingHigh.filter { it.bookId == bookId } +
                pending.filter { it.bookId == bookId }
            if (mine.isEmpty()) return
            val ordered = mine
                .distinctBy { it.chapterId }
                .sortedWith(
                    compareBy({ if (it.ordinal == startOrdinal) 0 else 1 },
                        { it.ordinal }),
                )
            ordered.forEach {
                notReadyAttempts.remove(it.chapterId)
                snoozedUntil.remove(it.chapterId)
            }
            // Убираем главы книги из обеих очередей и кладём их в начало быстрой.
            val restHigh = pendingHigh.filter { it.bookId != bookId }
            val restLow = pending.filter { it.bookId != bookId }
            pendingHigh = ConcurrentLinkedQueue(ordered + restHigh)
            pending = ConcurrentLinkedQueue(restLow)
            moved = ordered.size
        }
        _state.value = _state.value.copy(tasksPending = pendingSize())
        if (moved > 0) Log.d(TAG, "promoteBook bookId=$bookId moved=$moved to fast")
    }

    /**
     * Вышли из плеера с книгой [bookId] — её недокачанные главы уходят в
     * ФОНОВУЮ очередь (в хвост) с экспоненциальной задержкой: воспроизведение
     * их больше не ждёт, они не должны перебивать другие книги.
     */
    fun demoteBook(bookId: Int) {
        if (playerBookId == bookId) playerBookId = null
        var moved = 0
        synchronized(queueLock) {
            val mine = pendingHigh.filter { it.bookId == bookId } +
                pending.filter { it.bookId == bookId }
            if (mine.isEmpty()) return
            val ordered = mine.distinctBy { it.chapterId }.sortedBy { it.ordinal }
            ordered.forEach { t ->
                val attempts = (notReadyAttempts[t.chapterId] ?: 0) + 1
                notReadyAttempts[t.chapterId] = attempts
                val shift = (attempts - 1).coerceIn(0, 20)
                val backoffMs = minOf(
                    NOT_READY_DELAY_MS shl shift,
                    MAX_RETRY_DELAY_MS,
                )
                snoozedUntil[t.chapterId] = System.currentTimeMillis() + backoffMs
            }
            val restHigh = pendingHigh.filter { it.bookId != bookId }
            val restLow = pending.filter { it.bookId != bookId }
            pendingHigh = ConcurrentLinkedQueue(restHigh)
            pending = ConcurrentLinkedQueue(restLow + ordered)
            moved = ordered.size
        }
        _state.value = _state.value.copy(tasksPending = pendingSize())
        Log.d(TAG, "demoteBook bookId=$bookId moved=$moved to background")
    }

    /** Суммарный размер обеих очередей. */
    private fun pendingSize(): Int =
        synchronized(queueLock) { pending.size + pendingHigh.size }

    /** Поставить книгу целиком (все главы) в очередь. */
    fun enqueueBook(bookId: Int, chapters: List<ChapterDownloadSpec>) {
        for (spec in chapters) {
            enqueue(
                DownloadTask(
                    bookId = bookId,
                    chapterId = spec.chapterId,
                    ordinal = spec.ordinal,
                    sizeBytes = spec.sizeBytes,
                    url = spec.url,
                ),
            )
        }
    }

    /** Поставить раздачу целиком: для каждого тома берём главы из кэша и качаем. */
    suspend fun enqueueRelease(releaseId: Int, baseUrl: String) {
        val books = repository.getBooksByRelease(releaseId, excludeBookId = -1)
        for (book in books) {
            val chapters = repository.getChapters(book.id)
            enqueueBook(
                book.id,
                chapters.map { ch ->
                    ChapterDownloadSpec(
                        chapterId = ch.id,
                        ordinal = ch.ordinal,
                        sizeBytes = ch.sizeBytes,
                        url = absoluteUrl(baseUrl, "/books/${book.id}/stream/${ch.id}"),
                    )
                },
            )
        }
    }

    private suspend fun downloadChapter(task: DownloadTask): ChapterDownloadOutcome {
        val part = storage.partFile(task.bookId, task.ordinal)
        part.parentFile?.mkdirs()
        _state.value = _state.value.copy(
            isActive = true,
            currentBookId = task.bookId,
            currentChapterOrdinal = task.ordinal,
            totalBytes = task.sizeBytes,
        )
        repository.updateChapterLocal(task.chapterId, "downloading", 0)

        try {
            val existing = if (part.exists()) part.length() else 0L
            val range = if (existing >= task.sizeBytes) {
                // Уже скачано целиком.
                completeChapter(task, part)
                return ChapterDownloadOutcome.COMPLETE
            } else existing

            val request = Request.Builder()
                .url(task.url)
                .apply {
                    if (range > 0) header("Range", "bytes=$range-")
                }
                .build()

            httpClient.newCall(request).execute().use { resp ->
                if (!resp.isSuccessful) throw IOException("HTTP ${resp.code}")
                val raf = RandomAccessFile(part, "rw")
                raf.seek(range)
                resp.body!!.source().use { source ->
                    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                    var downloaded = range
                    while (true) {
                        if (!serverAvailable) {
                            // Приостанавливаем, сохраняя текущий размер.
                            syncAndClose(raf)
                            return ChapterDownloadOutcome.RETRY
                        }
                        val read = source.read(buffer)
                        if (read == -1) break
                        raf.write(buffer, 0, read)
                        downloaded += read
                        repository.updateChapterLocal(task.chapterId, "downloading", downloaded)
                        _state.value = _state.value.copy(downloadedBytes = downloaded)
                    }
                    syncAndClose(raf)
                }
            }
            completeChapter(task, part)
            return ChapterDownloadOutcome.COMPLETE
        } catch (e: Exception) {
            val downloaded = try { part.length() } catch (_: Exception) { 0L }
            val notReady = e.message?.contains("409") == true
            if (!notReady) {
                // Частичную докачку фиксируем только при реальном сетевом прогрессе.
                // На 409 глава ещё не готова на сервере — статус не портим.
                repository.updateChapterLocal(task.chapterId, "partial", downloaded)
            }
            Log.d(TAG, "downloadChapter failed book=${task.bookId} ord=${task.ordinal} " +
                "bytes=$downloaded err=${e.message}")
            return if (notReady) {
                ChapterDownloadOutcome.NOT_READY
            } else {
                ChapterDownloadOutcome.RETRY
            }
        } finally {
            _state.value = _state.value.copy(tasksPending = pendingSize())
        }
    }

    private suspend fun completeChapter(task: DownloadTask, part: File) {
        if (!serverAvailable) return
        val ready = storage.chapterFile(task.bookId, task.ordinal)
        ready.parentFile?.mkdirs()
        if (ready.exists()) ready.delete()
        part.renameTo(ready)
        repository.updateChapterLocal(task.chapterId, "ready", task.sizeBytes)

        // Реальную длительность знает только клиент по содержимому аудиофайла.
        // После завершения докачки измеряем длительность готового .mp3 и сохраняем
        // в кэш — верхний бар книги начинает показывать реальную сумму.
        val durationSec = ChapterDurationProbe.measureDurationSec(ready)
        Log.d(TAG, "completeChapter book=${task.bookId} ord=${task.ordinal} " +
            "durationSec=$durationSec")
        repository.setChapterLocalDuration(task.chapterId, durationSec)
    }

    private suspend fun markBookIfReady(bookId: Int) {
        val chapters = repository.getChapters(bookId)
        val allReady = chapters.isNotEmpty() && chapters.all { storage.isChapterReady(it) }
        val book = repository.getBookEntity(bookId)
        if (allReady && book != null) {
            repository.updateBookLocal(bookId, "ready", book.sizeBytes ?: 0, book.sizeBytes ?: 0)
            _onBookReady.value = bookId to (book.title)
        } else if (book != null) {
            val readyCount = chapters.count { storage.isChapterReady(it) }
            val total = book.sizeBytes ?: 0
            repository.updateBookLocal(bookId, "partial", total * readyCount / maxOf(1, chapters.size), total)
        }
    }

    fun stop() {
        worker?.cancel()
        worker = null
        synchronized(queueLock) {
            pending.clear()
            pendingHigh.clear()
        }
        activeTasks.clear()
        notReadyAttempts.clear()
        snoozedUntil.clear()
        playerBookId = null
        _state.value = DownloadQueueState()
    }

    private fun syncAndClose(raf: RandomAccessFile) {
        try {
            raf.fd.sync()
        } catch (_: IOException) {
        }
        try {
            raf.close()
        } catch (_: IOException) {
        }
    }

    companion object {
        private const val TAG = "DownloadQueue"
        private const val DEFAULT_BUFFER_SIZE = 8 * 1024

        /** Исход докачки одной главы. */
        private enum class ChapterDownloadOutcome {
            /** Глава скачана целиком. */
            COMPLETE,

            /** Сервер вернул 409 — глава ещё не READY. */
            NOT_READY,

            /** Временный сбой — короткий ретрай. */
            RETRY,
        }

        /**
         * Задержка повтора ГОЛОВЫ быстрой очереди — ФИКСИРОВАННАЯ (2с), без
         * роста. Голова повторяется каждые 2с, пока не скачается полностью
         * (до общего таймаута ожидания книги на клиенте); адаптивный рост
         * сделал бы быструю очередь медленной, поэтому его здесь нет.
         */
        private const val FAST_RETRY_DELAY_MS = 2_000L

        /** Пауза между повторами фоновой задачи при временном сбое. */
        private const val RETRY_DELAY_MS = 3_000L

        /** База бэкоффа карусели при 409 (10с): с каждой неудачей удваивается. */
        private const val NOT_READY_DELAY_MS = 10_000L

        /** Потолок бэкоффа карусели: не реже раза в сутки. */
        private const val MAX_RETRY_DELAY_MS = 86_400_000L

        /** Как часто воркер просыпается, когда фон «спит» (не блокирует очередь). */
        private const val SNOOZE_WAKE_MS = 1_000L

        private fun absoluteUrl(baseUrl: String, url: String): String =
            if (url.startsWith("http://") || url.startsWith("https://")) {
                url
            } else {
                baseUrl.trimEnd('/') + if (url.startsWith("/")) url else "/$url"
            }
    }
}
