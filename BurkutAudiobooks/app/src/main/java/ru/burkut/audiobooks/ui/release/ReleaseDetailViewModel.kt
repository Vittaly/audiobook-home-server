package ru.burkut.audiobooks.ui.release

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.ServerAvailability
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.ReleaseEntity

data class ReleaseUiState(
    val loading: Boolean = true,
    val error: String? = null,
    val release: ReleaseEntity? = null,
    val books: List<BookEntity> = emptyList(),
    val isServerOnline: Boolean = false,
    val queued: Boolean = false,
)

class ReleaseDetailViewModel(
    application: Application,
    private val releaseId: Int,
) : ViewModel() {

    private val app = application as App
    private val repository = app.repository

    private val _state = MutableStateFlow(ReleaseUiState())
    val state: StateFlow<ReleaseUiState> = _state.asStateFlow()

    init {
        viewModelScope.launch {
            repository.observeReleases().collect { list ->
                val rel = list.firstOrNull { it.id == releaseId }
                if (rel != null) {
                    _state.update {
                        it.copy(release = rel, loading = false, queued = rel.queuedForDevice)
                    }
                }
            }
        }
        viewModelScope.launch {
            repository.observeBooks(releaseId).collect { books ->
                _state.update { it.copy(books = books) }
            }
        }
        viewModelScope.launch {
            app.serverMonitor.availability.collect { avail ->
                _state.update { it.copy(isServerOnline = avail == ServerAvailability.Online) }
            }
        }
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _state.update { it.copy(loading = true, error = null) }
            try {
                repository.getReleaseDetail(
                    releaseId,
                    app.serverMonitor.availability.value == ServerAvailability.Online,
                )
            } catch (e: Exception) {
                _state.update { it.copy(error = e.message) }
            } finally {
                _state.update { it.copy(loading = false) }
            }
        }
    }

    /** Поставить раздачу целиком (все тома) в фоновую загрузку на устройство. */
    fun queueDownload() {
        viewModelScope.launch {
            repository.setReleaseQueued(releaseId, true)
            val base = app.settings.baseUrlOrNull()
            if (base != null) {
                app.downloadQueue.enqueueRelease(releaseId, base)
            }
            _state.update { it.copy(queued = true) }
        }
    }

    fun cancelQueue() {
        viewModelScope.launch {
            repository.setReleaseQueued(releaseId, false)
            _state.update { it.copy(queued = false) }
        }
    }
}