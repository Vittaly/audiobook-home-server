package ru.burkut.audiobooks.ui.catalog

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Clear
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import ru.burkut.audiobooks.data.db.ReleaseEntity
import ru.burkut.audiobooks.ui.appInstance
import ru.burkut.audiobooks.ui.util.formatReleaseDate
import ru.burkut.audiobooks.ui.util.formatSeconds
import ru.burkut.audiobooks.ui.viewModelFactoryOf

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CatalogScreen(
    onOpenRelease: (Int) -> Unit,
    onOpenSettings: () -> Unit,
    vm: CatalogViewModel = viewModel(
        factory = viewModelFactoryOf { CatalogViewModel(appInstance()) }
    ),
) {
    val state by vm.state.collectAsState()
    val isLoading by vm.isLoading.collectAsState()
    val isServerOnline by vm.isServerOnline.collectAsState()

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Раздачи") },
                actions = {
                    // Зелёный индикатор доступа к серверу.
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier.padding(end = 4.dp),
                    ) {
                        if (isServerOnline) {
                            Surface(
                                shape = CircleShape,
                                color = MaterialTheme.colorScheme.primary,
                                modifier = Modifier
                                    .size(10.dp)
                                    .align(Alignment.CenterVertically),
                            ) {}
                            Text(
                                "Сервер доступен",
                                style = MaterialTheme.typography.labelSmall,
                                modifier = Modifier.padding(start = 6.dp),
                            )
                        } else {
                            Surface(
                                shape = CircleShape,
                                color = MaterialTheme.colorScheme.error,
                                modifier = Modifier.size(10.dp),
                            ) {}
                            Text(
                                "Оффлайн",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.error,
                                modifier = Modifier.padding(start = 6.dp),
                            )
                        }
                    }
                    IconButton(onClick = vm::refresh) {
                        Icon(Icons.Default.Refresh, contentDescription = "Обновить")
                    }
                    IconButton(onClick = onOpenSettings) {
                        Icon(Icons.Default.Settings, contentDescription = "Сервер")
                    }
                },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize(),
        ) {
            // Баннер оффлайна: каталог из кэша.
            if (!isServerOnline) {
                Surface(
                    color = MaterialTheme.colorScheme.errorContainer,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(
                        "Сервер не доступен — показан кэш каталога. " +
                            "Слушать можно через «Скачать и слушать» оффлайн.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        modifier = Modifier.padding(horizontal = 12.dp, vertical = 8.dp),
                    )
                }
            }

            CatalogFiltersBar(
                filters = state.filters,
                availableRubrics = state.availableRubrics,
                availableSeries = state.availableSeries,
                onQueryChange = vm::setQuery,
                onRubricChange = vm::setRubric,
                onSeriesChange = vm::setSeries,
                onSortChange = vm::setSort,
                onReset = vm::resetFilters,
            )

            val hasAnyReleases = state.totalCount > 0
            val shown = state.releases

            LazyColumn(modifier = Modifier.fillMaxSize()) {
                items(shown, key = { it.id }) { release ->
                    ReleaseCard(
                        release = release,
                        onClick = { onOpenRelease(release.id) },
                    )
                }
                if (isLoading) {
                    item {
                        Box(
                            modifier = Modifier.fillMaxWidth().padding(16.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            CircularProgressIndicator()
                        }
                    }
                }
                if (shown.isEmpty() && !isLoading) {
                    item {
                        Box(
                            modifier = Modifier.fillMaxWidth().padding(32.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                                if (hasAnyReleases) {
                                    // Каталог не пуст — отфильтровано всё.
                                    Text("Ничего не найдено")
                                    Spacer(Modifier.height(8.dp))
                                    Text(
                                        "Измените поиск или сбросьте фильтры",
                                        style = MaterialTheme.typography.bodySmall,
                                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                    if (state.filters.isActive) {
                                        Spacer(Modifier.height(8.dp))
                                        TextButton(onClick = vm::resetFilters) {
                                            Text("Сбросить фильтры")
                                        }
                                    }
                                } else {
                                    Text("Каталог пуст")
                                    Spacer(Modifier.height(8.dp))
                                    TextButton(onClick = vm::refresh) {
                                        Text("Обновить")
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun CatalogFiltersBar(
    filters: CatalogFilters,
    availableRubrics: List<String>,
    availableSeries: List<SeriesOption>,
    onQueryChange: (String) -> Unit,
    onRubricChange: (String?) -> Unit,
    onSeriesChange: (Int?) -> Unit,
    onSortChange: (CatalogSort) -> Unit,
    onReset: () -> Unit,
) {
    Column(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp),
    ) {
        // Поиск по названию/автору.
        OutlinedTextField(
            value = filters.query,
            onValueChange = onQueryChange,
            singleLine = true,
            label = { Text("Поиск по названию или автору") },
            leadingIcon = {
                Icon(Icons.Default.Search, contentDescription = null)
            },
            trailingIcon = {
                if (filters.query.isNotEmpty()) {
                    IconButton(onClick = { onQueryChange("") }) {
                        Icon(Icons.Default.Clear, contentDescription = "Очистить")
                    }
                }
            },
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Search),
            modifier = Modifier.fillMaxWidth(),
        )

        // Сортировка.
        Spacer(Modifier.height(8.dp))
        Text(
            "Сортировка",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = filters.sort == CatalogSort.DATE_DESC,
                onClick = { onSortChange(CatalogSort.DATE_DESC) },
                label = { Text("Сначала новые") },
            )
            FilterChip(
                selected = filters.sort == CatalogSort.TITLE_ASC,
                onClick = { onSortChange(CatalogSort.TITLE_ASC) },
                label = { Text("По названию") },
            )
            FilterChip(
                selected = filters.sort == CatalogSort.YEAR_DESC,
                onClick = { onSortChange(CatalogSort.YEAR_DESC) },
                label = { Text("По году") },
            )
        }

        // Фильтр по рубрикам (категориям).
        if (availableRubrics.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(
                "Категории",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            LazyRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = PaddingValues(vertical = 4.dp),
            ) {
                items(availableRubrics, key = { it }) { rubric ->
                    FilterChip(
                        selected = filters.rubric == rubric,
                        onClick = { onRubricChange(rubric) },
                        label = { Text(rubric) },
                    )
                }
            }
        }

        // Фильтр по сериям (доступны для раздач, чьи тома уже в кэше).
        if (availableSeries.isNotEmpty()) {
            Text(
                "Серии",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            LazyRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = PaddingValues(vertical = 4.dp),
            ) {
                items(availableSeries, key = { it.id }) { series ->
                    FilterChip(
                        selected = filters.seriesId == series.id,
                        onClick = { onSeriesChange(series.id) },
                        label = { Text(series.title) },
                    )
                }
            }
        }

        if (filters.isActive) {
            Row(horizontalArrangement = Arrangement.End, modifier = Modifier.fillMaxWidth()) {
                TextButton(onClick = onReset) {
                    Text("Сбросить фильтры")
                }
            }
        }
    }
}

@Composable
private fun ReleaseCard(
    release: ReleaseEntity,
    onClick: () -> Unit,
) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 6.dp)
            .clickable(onClick = onClick),
        elevation = CardDefaults.cardElevation(defaultElevation = 2.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            modifier = Modifier.padding(12.dp),
        ) {
            AsyncImage(
                model = release.coverUrl,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier
                    .size(72.dp)
                    .clip(CircleShape),
            )
            Column(
                modifier = Modifier
                    .weight(1f)
                    .padding(start = 12.dp),
            ) {
                Text(
                    release.title,
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 2,
                )
                val meta = buildList {
                    release.author?.let { add(it) }
                    // Дата релиза с трекера — основная дата каталога.
                    formatReleaseDate(release.releasedAt)?.let { add(it) }
                    // Год написания/издания книги — дополнительная информация.
                    release.year?.let { add("$it г.") }
                    release.totalDurationSec?.let { add(formatSeconds(it)) }
                    add("Томов: ${release.booksCount}")
                }.joinToString(" · ")
                if (meta.isNotEmpty()) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        meta,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }
}
