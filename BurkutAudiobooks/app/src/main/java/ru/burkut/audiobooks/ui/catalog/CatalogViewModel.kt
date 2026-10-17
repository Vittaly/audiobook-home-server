package ru.burkut.audiobooks.ui.catalog

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.debounce
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import retrofit2.HttpException
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.api.BookOut
import java.io.IOException

class CatalogViewModel(app: Application) : ViewModel() {

    private val repository = (app as App).repository

    val query = MutableStateFlow("")
    val rubric = MutableStateFlow<String?>(null)

    private val _books = MutableStateFlow<List<BookOut>>(emptyList())
    val books: StateFlow<List<BookOut>> = _books.asStateFlow()

    private val _rubrics = MutableStateFlow<List<String>>(emptyList())
    val rubrics: StateFlow<List<String>> = _rubrics.asStateFlow()

    private val _total = MutableStateFlow(0)
    val total: StateFlow<Int> = _total.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _error = MutableStateFlow<String?>(null)
    val error: StateFlow<String?> = _error.asStateFlow()

    private var offset = 0
    private var hasMore = true
    private var loading = false

    init {
        viewModelScope.launch {
            combine(query.debounce(350), rubric) { q, r -> q to r }
                .distinctUntilChanged()
                .collect { (q, r) -> reload(q, r) }
        }
    }

    fun selectRubric(r: String?) {
        rubric.value = if (rubric.value == r) null else r
    }

    fun refresh() {
        viewModelScope.launch { reload(query.value, rubric.value) }
    }

    fun loadMore() {
        viewModelScope.launch { loadNext(query.value, rubric.value) }
    }

    private suspend fun reload(q: String, r: String?) {
        offset = 0
        hasMore = true
        _books.value = emptyList()
        _rubrics.value = emptyList()
        _error.value = null
        loadNext(q, r)
    }

    private suspend fun loadNext(q: String, r: String?) {
        if (loading || !hasMore) return
        val first = offset == 0
        loading = true
        _isLoading.value = true
        try {
            val resp = repository.listBooks(
                query = q.ifEmpty { null },
                rubric = r,
                limit = PAGE_SIZE,
                offset = offset,
            )
            offset += resp.items.size
            hasMore = resp.items.size == PAGE_SIZE && offset < resp.total
            _total.value = resp.total
            _books.update { old -> if (first) resp.items else old + resp.items }
            _rubrics.update { old -> (old + resp.items.flatMap { it.rubrics }).distinct().sorted() }
        } catch (e: IOException) {
            if (_books.value.isEmpty()) _error.value = "Нет соединения с сервером"
        } catch (e: HttpException) {
            if (_books.value.isEmpty()) _error.value = "Ошибка сервера: ${e.code()}"
        } catch (e: IllegalStateException) {
            _error.value = e.message
        } finally {
            loading = false
            _isLoading.value = false
        }
    }

    companion object {
        private const val PAGE_SIZE = 50
    }
}
