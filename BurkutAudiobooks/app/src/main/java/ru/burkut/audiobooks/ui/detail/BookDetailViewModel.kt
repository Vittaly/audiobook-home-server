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
    val progress: ProgressEntity? = null,
)

class BookDetailViewModel(
    application: Application,
    private val bookHash: String,
) : ViewModel() {

    private val app: App = application as App
    private val repository = app.repository
    private val storage = app.storage

    private val _state = MutableStateFlow(BookDetailUiState())
    val state: StateFlow<BookDetailUiState> = _state.asStateFlow()

    private var pollJob: kotlinx.coroutines.Job? = null

    init {
        // Книга из локального кэша (оффлайн-first).
        viewModelScope.launch {
            val book = repository.getBookEntity(bookHash)
            _state.update { it.copy(book = book, loading = book == null) }
            refresh()
        }

        // Главы из кэша + статус устройства пересчитываем по ним.
        viewModelScope.launch {
            repository.observeChapters(bookHash).collect { chapters ->
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
            val book = repository.getBookEntity(bookHash)
            val progress = repository.getProgress(bookHash)
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
            val detail = repository.getBookDetail(bookHash, online)
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
                    repository.getDownloadStatus(bookHash)
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

    /** Удалить локальные файлы книги с устройства. */
    fun deleteFromDevice() {
        viewModelScope.launch {
            val book = _state.value.book ?: return@launch
            // Удаляем файлы с устройства.
            runCatching { storage.bookDir(bookHash).deleteRecursively() }
            // Сбрасываем локальный статус глав и книги.
            val chapters = repository.getChapters(bookHash)
            for (ch in chapters) {
                repository.updateChapterLocal(ch.id, "absent", 0)
            }
            repository.updateBookLocal(bookHash, "absent", 0, book.sizeBytes ?: 0)
            repository.clearProgress(bookHash)
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