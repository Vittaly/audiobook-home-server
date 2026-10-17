package ru.burkut.audiobooks.ui.catalog

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.db.ReleaseEntity

class CatalogViewModel(application: Application) : ViewModel() {

    private val app = application as App
    private val repository = app.repository

    /** Каталог раздач из кэша (работает оффлайн). */
    private val _releases = MutableStateFlow<List<ReleaseEntity>>(emptyList())
    val releases: StateFlow<List<ReleaseEntity>> = _releases.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _isServerOnline = MutableStateFlow(false)
    val isServerOnline: StateFlow<Boolean> = _isServerOnline.asStateFlow()

    private val _cachedAtMinute = MutableStateFlow(0L)
    val cachedAtMinute: StateFlow<Long> = _cachedAtMinute.asStateFlow()

    init {
        viewModelScope.launch {
            repository.observeReleases().collect { list ->
                _releases.value = list
            }
        }
        viewModelScope.launch {
            app.serverMonitor.availability.collect { avail ->
                _isServerOnline.value =
                    avail == ru.burkut.audiobooks.data.ServerAvailability.Online
            }
        }
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            // Всегда читаем кэш, а поверх — обновляем снапшот с сервера, если он доступен.
            _isLoading.value = true
            try {
                runCatching { repository.syncReleases() }
                _cachedAtMinute.value = System.currentTimeMillis() / 60_000
            } finally {
                _isLoading.value = false
            }
        }
    }
}
