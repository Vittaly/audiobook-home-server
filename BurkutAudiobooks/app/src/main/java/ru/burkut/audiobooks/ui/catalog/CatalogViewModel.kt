 в package ru.burkut.audiobooks.ui.catalog

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.delay
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.ServerAvailability
import ru.burkut.audiobooks.data.db.BookEntity

/** Режим сортировки каталога книг. */
enum class CatalogSort {
    /** По дате добавления: сначала самые свежие. */
    DATE_DESC,

    /** По названию, по возрастанию. */
    TITLE_ASC,

    /** По году, сначала более новые. */
    YEAR_DESC,
}

/** Опция фильтра по серии (строится из кэша книг). */
data class SeriesOption(val id: Int, val title: String)

/** Состояние фильтрации каталога. */
data class CatalogFilters(
    val query: String = "",
    val rubric: String? = null,
    val seriesId: Int? = null,
    val sort: CatalogSort = CatalogSort.DATE_DESC,
) {
    /** Активен ли хотя бы один фильтр/поиск (сортировка не считается). */
    val isActive: Boolean get() = query.isNotBlank() || rubric != null || seriesId != null
}

/** Готовое к отрисовке состояние экрана каталога. */
data class CatalogUiState(
    val books: List<BookEntity> = emptyList(),
    val totalCount: Int = 0,
    val availableRubrics: List<String> = emptyList(),
    val availableSeries: List<SeriesOption> = emptyList(),
    val filters: CatalogFilters = CatalogFilters(),
    val isServerOnline: Boolean = false,
    /** id книг, у которых первая глава уже скачана на устройство (зелёная точка). */
    val firstChapterReadyIds: Set<String> = emptySet(),
)

/**
 * Каталог книг: cache-first.
 *
 * Данные читаются только из Room (работает оффлайн). Поиск по названию/автору,
 * фильтрация по рубрикам и сериям, а также сортировка выполняются на клиенте,
 * поверх кэша — сервер для этого не требуется.
 */
class CatalogViewModel(application: Application) : ViewModel() {

    private val app = application as App
    private val repository = app.repository
    private val storage = app.storage

    private val _filters = MutableStateFlow(CatalogFilters())
    val filters: StateFlow<CatalogFilters> = _filters.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _isServerOnline = MutableStateFlow(false)
    val isServerOnline: StateFlow<Boolean> = _isServerOnline.asStateFlow()

    private val _cachedAtMinute = MutableStateFlow(0L)
    val cachedAtMinute: StateFlow<Long> = _cachedAtMinute.asStateFlow()

    // id книг, у которых первая глава реально скачана на устройство (файл на диске).
    // Главы не пишутся в Room (в каталог синкаются только книги), поэтому
    // единственный надёжный источник готовности — проверка файла {ordinal}.mp3.
    private val _firstChapterReadyIds = MutableStateFlow<Set<String>>(emptySet())

    // Все книги из кэша (до фильтрации).
    private val allBooks: StateFlow<List<BookEntity>> =
        repository.observeAllBooks()
            .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    /** Единое состояние экрана: применяет поиск, фильтры и сортировку к кэшу. */
    val state: StateFlow<CatalogUiState> =
        combine(
            allBooks,
            _filters,
            _isLoading,
            _isServerOnline,
            _firstChapterReadyIds,
        ) { books, filters, _loadingUnused, online, firstChapterReadyIds ->
            CatalogUiState(
                books = applyFilters(books, filters),
                totalCount = books.size,
                availableRubrics = rubricOptions(books),
                availableSeries = seriesOptions(books),
                filters = filters,
                isServerOnline = online,
                firstChapterReadyIds = firstChapterReadyIds,
            )
        }.stateIn(
            viewModelScope,
            SharingStarted.Eagerly,
            CatalogUiState(),
        )

    init {
        viewModelScope.launch {
            app.serverMonitor.availability.collect { avail ->
                _isServerOnline.value = avail == ServerAvailability.Online
            }
        }
        // Каталог может измениться (синк) — пересчитываем набор готовых первых глав.
        viewModelScope.launch {
            allBooks.collect { books -> refreshFirstChapterReady(books) }
        }
        // Файлы первой главы появляются при скачивании «на лету» при прослушивании,
        // а не при синке каталога — опрашиваем диск периодически, пока экран жив.
        viewModelScope.launch {
            while (true) {
                delay(FIRST_CHAPTER_POLL_MS)
                refreshFirstChapterReady(allBooks.value)
            }
        }
        refresh()
    }

    /** Пересчитывает набор книг с готовой первой главой (лёгкая проверка файлов). */
    private fun refreshFirstChapterReady(books: List<BookEntity>) {
        val readyIds = books
            .asSequence()
            .filter { storage.isFirstChapterReady(it.id) }
            .map { it.id }
            .toSet()
        _firstChapterReadyIds.value = readyIds
    }

    fun refresh() {
        viewModelScope.launch {
            _isLoading.value = true
            try {
                runCatching { repository.syncBooks() }
                _cachedAtMinute.value = System.currentTimeMillis() / 60_000
            } finally {
                _isLoading.value = false
            }
        }
    }

    // ---------- Изменение фильтров ----------

    fun setQuery(query: String) {
        _filters.update { it.copy(query = query) }
    }

    /** Повторное нажатие на ту же рубрику снимает фильтр. */
    fun setRubric(rubric: String?) {
        _filters.update { it.copy(rubric = if (it.rubric == rubric) null else rubric) }
    }

    /** Повторное нажатие на ту же серию снимает фильтр. */
    fun setSeries(seriesId: Int?) {
        _filters.update { it.copy(seriesId = if (it.seriesId == seriesId) null else seriesId) }
    }

    fun setSort(sort: CatalogSort) {
        _filters.update { it.copy(sort = sort) }
    }

    /** Сбрасывает поиск и фильтры (сортировку сохраняет). */
    fun resetFilters() {
        _filters.update { it.copy(query = "", rubric = null, seriesId = null) }
    }

    // ---------- Чистые функции фильтрации/сортировки ----------

    private fun applyFilters(
        books: List<BookEntity>,
        filters: CatalogFilters,
    ): List<BookEntity> {
        val q = filters.query.trim().lowercase()

        return books
            .asSequence()
            .filter { book ->
                q.isEmpty() ||
                    book.title.lowercase().contains(q) ||
                    book.author?.lowercase()?.contains(q) == true
            }
            .filter { book ->
                val rubric = filters.rubric
                rubric == null || book.rubricList().any { it.equals(rubric, ignoreCase = true) }
            }
            .filter { book ->
                val seriesId = filters.seriesId
                seriesId == null || book.seriesId == seriesId
            }
            .sortedWith(comparatorFor(filters.sort))
            .toList()
    }

    private fun comparatorFor(sort: CatalogSort): Comparator<BookEntity> = when (sort) {
        // addedAt — дата добавления книги (ISO-8601): обратная сортировка даёт
        // свежие сверху.
        CatalogSort.DATE_DESC -> compareByDescending { it.addedAt }
        CatalogSort.TITLE_ASC -> compareBy { it.title.lowercase() }
        CatalogSort.YEAR_DESC -> compareByDescending { it.year ?: 0 }
    }

    /** Список доступных рубрик каталога (из кэша книг). */
    private fun rubricOptions(books: List<BookEntity>): List<String> =
        books
            .flatMap { it.rubricList() }
            .filter { it.isNotBlank() }
            .distinctBy { it.lowercase() }
            .sorted()

    /** Список доступных серий (из кэша книг, чьи тома уже загружены). */
    private fun seriesOptions(books: List<BookEntity>): List<SeriesOption> =
        books
            .mapNotNull { book ->
                val id = book.seriesId ?: return@mapNotNull null
                val title = book.seriesTitle?.takeIf { it.isNotBlank() } ?: return@mapNotNull null
                SeriesOption(id, title)
            }
            .distinctBy { it.id }
            .sortedBy { it.title.lowercase() }

    companion object {
        /** Период опроса диска на предмет появления первой главы книги. */
        private const val FIRST_CHAPTER_POLL_MS = 2_000L
    }
}

/** Рубрики книги хранятся в кэше одной строкой, разделённой переводами строк. */
private fun BookEntity.rubricList(): List<String> =
    rubrics?.split("\n")?.map { it.trim() }?.filter { it.isNotEmpty() } ?: emptyList()
