package ru.burkut.audiobooks.player

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
 * Последовательная очередь загрузки глав на устройство.
 *
 * - Каждая глава качается с сервера (stream_url, поддержка Range для докачки).
 * - Пишется в `{ordinal}.mp3.part`, по завершении переименовывается в `{ordinal}.mp3`.
 * - Статусы и байты обновляются в Room (localStatus главы и книги).
 * - Пауза/возобновление управляется извне через [setServerAvailable].
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

    private val pending = ConcurrentLinkedQueue<DownloadTask>()
    private val activeTasks = mutableMapOf<Int, Boolean>() // chapterId -> running

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
            while (true) {
                if (!serverAvailable) {
                    // Ждём появления доступа.
                    delay(500)
                    continue
                }
                val task = pending.poll()
                if (task == null) {
                    _state.value = _state.value.copy(isActive = false)
                    delay(1000)
                    continue
                }
                val downloaded = downloadChapter(task)
                if (downloaded) {
                    // Глава скачана полностью — проверим, готова ли вся книга.
                    markBookIfReady(task.bookId)
                }
            }
        }
    }

    /** Добавить главу в очередь (идемпотентно по chapterId). */
    fun enqueue(task: DownloadTask) {
        if (activeTasks.putIfAbsent(task.chapterId, true) != null) return
        pending.add(task)
        _state.value = _state.value.copy(tasksPending = pending.size)
        if (serverAvailable) ensureWorker()
    }

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

    private suspend fun downloadChapter(task: DownloadTask): Boolean {
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
                return true
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
                            return false
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
            return true
        } catch (e: Exception) {
            repository.updateChapterLocal(task.chapterId, "partial", try { part.length() } catch (_: Exception) { 0L })
            return false
        } finally {
            activeTasks.remove(task.chapterId)
            _state.value = _state.value.copy(tasksPending = pending.size)
        }
    }

    private suspend fun completeChapter(task: DownloadTask, part: File) {
        if (!serverAvailable) return
        storage.chapterFile(task.bookId, task.ordinal).let { ready ->
            ready.parentFile?.mkdirs()
            if (ready.exists()) ready.delete()
            part.renameTo(ready)
        }
        repository.updateChapterLocal(task.chapterId, "ready", task.sizeBytes)
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
        pending.clear()
        activeTasks.clear()
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
        private const val DEFAULT_BUFFER_SIZE = 8 * 1024

        private fun absoluteUrl(baseUrl: String, url: String): String =
            if (url.startsWith("http://") || url.startsWith("https://")) {
                url
            } else {
                baseUrl.trimEnd('/') + if (url.startsWith("/")) url else "/$url"
            }
    }
}
