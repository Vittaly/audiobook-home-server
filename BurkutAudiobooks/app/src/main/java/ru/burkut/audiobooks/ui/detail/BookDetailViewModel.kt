package ru.burkut.audiobooks.ui.detail

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.ServerAvailability
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.api.DownloadStatusOut
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.ChapterEntity
import ru.burkut.audiobooks.data.db.ProgressEntity

/** Состояние карточки книги (тома). */
data class BookDetailUiState(
    val loading: Boolean = true,
    val error: String? = null,
    val book: BookEntity? = null,
    val chapters: List<ChapterEntity> = emptyList(),
    val isServerOnline: Boolean = false,
    /** Статус книги на сервере (getBookDetail / getDownloadStatus). */
    val serverStatus: BookStatus? = null,
    /** Детали загрузки на сервере (когда идёт качание торрента). */
    val serverDownload: DownloadStatusOut? = null,
    /** Локальный статус книги на устройстве: absent | downloading | partial | ready. */
    val deviceStatus: String = "absent",
    /** Раздача поставлена в фоновую докачку на устройство. */
    val queued: Boolean = false,
    val progress: ProgressEntity? = null,
)

class BookDetailViewModel(
    application: Application,
    private val bookId: Int,
) : ViewModel() {

    private val app: App = application as App
    private val repository = app.repository
    private val storage = app.storage
    private val downloadQueue = app.downloadQueue

    private val _state = MutableStateFlow(BookDetailUiState())
    val state: StateFlow<BookDetailUiState> = _state.asStateFlow()

    private var pollJob: kotlinx.coroutines.Job? = null

    init {
        // Книга из локального кэша (оффлайн-first).
        viewModelScope.launch {
            val book = repository.getBookEntity(bookId)
            _state.update { it.copy(book = book, loading = book == null) }
            if (book != null) {
                // Флаг «в очереди» хранится на раздаче.
                val rel = repository.getReleaseEntity(book.releaseId)
                _state.update { it.copy(queued = rel?.queuedForDevice ?: false) }
            }
            refresh()
        }

        // Главы из кэша + статус устройства пересчитываем по ним.
        viewModelScope.launch {
            repository.observeChapters(bookId).collect { chapters ->
                _state.update { it.copy(chapters = chapters, deviceStatus = deviceStatusOf(chapters)) }
            }
        }

        // Доступность сервера — для индикатора и сетевых статусов.
        viewModelScope.launch {
            app.serverMonitor.availability.collect { avail ->
                val online = avail == ServerAvailability.Online
                _state.update { it.copy(isServerOnline = online) }
                if (online) refreshServerStatus() else stopServerPolling()
            }
        }
    }

    /** Полное обновление: книга + прогресс + серверный статус. */
    fun refresh() {
        viewModelScope.launch {
            val book = repository.getBookEntity(bookId)
            val progress = repository.getProgress(bookId)
            _state.update { it.copy(book = book, loading = false, error = null, progress = progress) }
            refreshServerStatus()
        }
    }

    private fun refreshServerStatus() {
        viewModelScope.launch {
            val online = _state.value.isServerOnline
            if (!online) {
                _state.update { it.copy(serverStatus = null, serverDownload = null) }
                stopServerPolling()
                return@launch
            }
            // Свежая карточка книги с сервера (статус загрузки сервером).
            val detail = repository.getBookDetail(bookId, online)
            if (detail != null) {
                _state.update { st -> st.copy(serverStatus = detail.status) }
            }
            val status = _state.value.serverStatus
            if (status == BookStatus.downloading || status == BookStatus.partial) {
                startServerPolling()
            } else {
                _state.update { it.copy(serverDownload = null) }
                stopServerPolling()
            }
        }
    }

    private fun startServerPolling() {
        if (pollJob?.isActive == true) return
        pollJob = viewModelScope.launch {
            while (true) {
                val st = try {
                    repository.getDownloadStatus(bookId)
                } catch (e: Exception) {
                    null
                }
                if (st != null) {
                    _state.update { it.copy(serverDownload = st) }
                    if (st.status != BookStatus.downloading && st.status != BookStatus.partial) {
                        _state.update { it.copy(serverStatus = st.status) }
                        stopServerPolling()
                        return@launch
                    }
                }
                delay(2000)
            }
        }
    }

    private fun stopServerPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    /** Запросить у сервера начало загрузки книги (торрент). */
    fun startServerDownload() {
        viewModelScope.launch {
            try {
                repository.startDownload(bookId)
                refreshServerStatus()
            } catch (e: Exception) {
                _state.update { it.copy(error = e.message) }
            }
        }
    }

    fun stopServerDownload() {
        viewModelScope.launch {
            runCatching { repository.stopDownload(bookId) }
            _state.update { it.copy(serverDownload = null) }
            refresh()
        }
    }

    /** Поставить раздачу (все тома) в фоновую докачку на устройство. */
    fun queueReleaseDownload() {
        viewModelScope.launch {
            val book = _state.value.book ?: return@launch
            repository.setReleaseQueued(book.releaseId, true)
            val base = app.settings.baseUrlOrNull()
            if (base != null) downloadQueue.enqueueRelease(book.releaseId, base)
            _state.update { it.copy(queued = true) }
        }
    }

    fun cancelQueue() {
        viewModelScope.launch {
            val book = _state.value.book ?: return@launch
            repository.setReleaseQueued(book.releaseId, false)
            _state.update { it.copy(queued = false) }
        }
    }

    /** Удалить локальные файлы книги с устройства (сам торрент на сервере не трогаем). */
    fun deleteFromDevice() {
        viewModelScope.launch {
            val book = _state.value.book ?: return@launch
            // Удаляем файлы с устройства.
            runCatching { storage.bookDir(bookId).deleteRecursively() }
            // Сбрасываем локальный статус глав и книги.
            val chapters = repository.getChapters(bookId)
            for (ch in chapters) {
                repository.updateChapterLocal(ch.id, "absent", 0)
            }
            repository.updateBookLocal(bookId, "absent", 0, book.sizeBytes ?: 0)
            repository.clearProgress(bookId)
            refresh()
        }
    }

    private fun deviceStatusOf(chapters: List<ChapterEntity>): String {
        if (chapters.isEmpty()) return "absent"
        var ready = 0
        var downloading = 0
        for (ch in chapters) {
            when (ch.localStatus) {
                "ready" -> ready++
                "downloading", "partial" -> downloading++
            }
        }
        return when {
            ready == chapters.size -> "ready"
            downloading > 0 -> "downloading"
            ready > 0 -> "partial"
            else -> "absent"
        }
    }

    override fun onCleared() {
        stopServerPolling()
        super.onCleared()
    }
}