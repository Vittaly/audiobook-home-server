package ru.burkut.audiobooks.ui.catalog

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
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.ServerAvailability
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.ReleaseEntity

/** Режим сортировки каталога раздач. */
enum class CatalogSort {
    /** По дате релиза: сначала самые свежие. */
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
    val releases: List<ReleaseEntity> = emptyList(),
    val totalCount: Int = 0,
    val availableRubrics: List<String> = emptyList(),
    val availableSeries: List<SeriesOption> = emptyList(),
    val filters: CatalogFilters = CatalogFilters(),
    val isServerOnline: Boolean = false,
)

/**
 * Каталог раздач: cache-first.
 *
 * Данные читаются только из Room (работает оффлайн). Поиск по названию/автору,
 * фильтрация по рубрикам и сериям, а также сортировка выполняются на клиенте,
 * поверх кэша — сервер для этого не требуется.
 */
class CatalogViewModel(application: Application) : ViewModel() {

    private val app = application as App
    private val repository = app.repository

    private val _filters = MutableStateFlow(CatalogFilters())
    val filters: StateFlow<CatalogFilters> = _filters.asStateFlow()

    private val _isLoading = MutableStateFlow(false)
    val isLoading: StateFlow<Boolean> = _isLoading.asStateFlow()

    private val _isServerOnline = MutableStateFlow(false)
    val isServerOnline: StateFlow<Boolean> = _isServerOnline.asStateFlow()

    private val _cachedAtMinute = MutableStateFlow(0L)
    val cachedAtMinute: StateFlow<Long> = _cachedAtMinute.asStateFlow()

    // Все раздачи и книги из кэша (до фильтрации).
    private val allReleases: StateFlow<List<ReleaseEntity>> =
        repository.observeReleases()
            .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    private val allBooks: StateFlow<List<BookEntity>> =
        repository.observeAllBooks()
            .stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    /** Единое состояние экрана: применяет поиск, фильтры и сортировку к кэшу. */
    val state: StateFlow<CatalogUiState> =
        combine(
            allReleases,
            allBooks,
            _filters,
            _isLoading,
            _isServerOnline,
        ) { releases, books, filters, _loadingUnused, online ->
            CatalogUiState(
                releases = applyFilters(releases, books, filters),
                totalCount = releases.size,
                availableRubrics = rubricOptions(releases),
                availableSeries = seriesOptions(books),
                filters = filters,
                isServerOnline = online,
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
        refresh()
    }

    fun refresh() {
        viewModelScope.launch {
            _isLoading.value = true
            try {
                runCatching { repository.syncReleases() }
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
        releases: List<ReleaseEntity>,
        books: List<BookEntity>,
        filters: CatalogFilters,
    ): List<ReleaseEntity> {
        // Сопоставление раздача -> множество id серий её книг (из кэша).
        val seriesByRelease: Map<Int, Set<Int>> = books
            .mapNotNull { book -> book.seriesId?.let { book.releaseId to it } }
            .groupBy({ it.first }, { it.second })
            .mapValues { (_, ids) -> ids.toSet() }

        val q = filters.query.trim().lowercase()

        return releases
            .asSequence()
            .filter { rel ->
                q.isEmpty() ||
                    rel.title.lowercase().contains(q) ||
                    rel.author?.lowercase()?.contains(q) == true
            }
            .filter { rel ->
                val rubric = filters.rubric
                rubric == null || rel.rubricList().any { it.equals(rubric, ignoreCase = true) }
            }
            .filter { rel ->
                val seriesId = filters.seriesId
                seriesId == null || seriesByRelease[rel.id]?.contains(seriesId) == true
            }
            .sortedWith(comparatorFor(filters.sort))
            .toList()
    }

    private fun comparatorFor(sort: CatalogSort): Comparator<ReleaseEntity> = when (sort) {
        // releasedAt — дата создания раздачи на трекере (ISO-8601): лексикографический
        // порядок совпадает с хронологическим, поэтому обратная сортировка даёт
        // свежие сверху. Если даты трекера нет — откатываемся к addedAt.
        CatalogSort.DATE_DESC -> compareByDescending { it.releasedAt ?: it.addedAt }
        CatalogSort.TITLE_ASC -> compareBy { it.title.lowercase() }
        CatalogSort.YEAR_DESC -> compareByDescending { it.year ?: 0 }
    }

    /** Список доступных рубрик каталога (из кэша раздач). */
    private fun rubricOptions(releases: List<ReleaseEntity>): List<String> =
        releases
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
}

/** Рубрики раздачи хранятся в кэше одной строкой, разделённой переводами строк. */
private fun ReleaseEntity.rubricList(): List<String> =
    rubrics?.split("\n")?.map { it.trim() }?.filter { it.isNotEmpty() } ?: emptyList()
