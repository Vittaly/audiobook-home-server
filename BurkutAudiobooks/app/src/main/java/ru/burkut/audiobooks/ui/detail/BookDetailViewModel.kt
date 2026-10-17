@file:OptIn(androidx.media3.common.util.UnstableApi::class)

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
import retrofit2.HttpException
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.api.BookDetailOut
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.api.DownloadStatusOut
import ru.burkut.audiobooks.data.db.ProgressEntity
import java.io.IOException

data class DetailState(
    val loading: Boolean = true,
    val error: String? = null,
    val book: BookDetailOut? = null,
    val download: DownloadStatusOut? = null,
    val progress: ProgressEntity? = null,
    val cache: Map<String, androidx.media3.exoplayer.offline.Download> = emptyMap(),
)

class BookDetailViewModel(
    application: Application,
    private val bookId: Int,
) : ViewModel() {

    private val app: App = application as App
    private val repository = app.repository

    private val _state = MutableStateFlow<DetailState>(DetailState())
    val state: StateFlow<DetailState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            app.downloadObserver.downloads.collect { cache ->
                _state.update { it.copy(cache = cache) }
            }
        }
        load()
    }

    private fun load() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true, error = null) }
            try {
                val book = repository.getBook(bookId)
                val progress = repository.getProgress(bookId)
                _state.update { it.copy(loading = false, book = book, progress = progress) }
                if (book.status == BookStatus.downloading || book.status == BookStatus.partial) {
                    pollDownloadStatus()
                }
            } catch (e: IOException) {
                _state.update { it.copy(loading = false, error = "Нет соединения с сервером") }
            } catch (e: HttpException) {
                _state.update { it.copy(loading = false, error = "Ошибка сервера: ${e.code()}") }
            } catch (e: IllegalStateException) {
                _state.update { it.copy(loading = false, error = e.message) }
            }
        }
    }

    private suspend fun pollDownloadStatus() {
        while (true) {
            val st = try {
                repository.getDownloadStatus(bookId)
            } catch (e: Exception) {
                return
            }
            _state.update { it.copy(download = st) }
            if (st.status != BookStatus.downloading && st.status != BookStatus.partial) return
            delay(2000)
        }
    }

    fun startDownload() {
        viewModelScope.launch {
            try {
                repository.startDownload(bookId)
                pollDownloadStatus()
            } catch (e: HttpException) {
                if (e.code() == 409) {
                    pollDownloadStatus()
                } else {
                    _state.update { it.copy(error = "Ошибка сервера: ${e.code()}") }
                }
            } catch (e: Exception) {
                _state.update { it.copy(error = e.message) }
            }
        }
    }

    fun stopDownload() {
        viewModelScope.launch {
            runCatching { repository.stopDownload(bookId) }
            load()
        }
    }

    fun deleteBook(onDeleted: () -> Unit = {}) {
        val book = _state.value.book ?: return
        app.bookDownloads.removeBook(book)
        viewModelScope.launch {
            try {
                repository.clearProgress(bookId)
                repository.deleteBook(bookId)
                onDeleted()
            } catch (e: Exception) {
                _state.update { it.copy(error = e.message) }
            }
        }
    }

    fun downloadToCache() {
        val book = _state.value.book ?: return
        val base = app.settings.baseUrlOrNull() ?: return
        app.bookDownloads.enqueueBook(book, base)
    }

    fun removeFromCache() {
        val book = _state.value.book ?: return
        app.bookDownloads.removeBook(book)
    }

    fun refresh() = load()
}