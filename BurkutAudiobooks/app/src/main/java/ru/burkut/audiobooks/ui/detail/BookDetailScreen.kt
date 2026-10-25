package ru.burkut.audiobooks.ui.detail

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import ru.burkut.audiobooks.R
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.player.absoluteUrl
import ru.burkut.audiobooks.ui.appInstance
import ru.burkut.audiobooks.ui.theme.serverOnlineColor
import ru.burkut.audiobooks.ui.util.formatBytes
import ru.burkut.audiobooks.ui.util.formatDurationMs
import ru.burkut.audiobooks.ui.util.formatSeconds
import ru.burkut.audiobooks.ui.util.rememberBaseUrl
import ru.burkut.audiobooks.ui.viewModelFactoryOf

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BookDetailScreen(
    bookId: Int,
    onBack: () -> Unit,
    onPlay: (Int) -> Unit,
    vm: BookDetailViewModel = viewModel(
        key = "book-$bookId",
        factory = viewModelFactoryOf { BookDetailViewModel(appInstance(), bookId) },
    ),
) {
    val state by vm.state.collectAsState()
    val baseUrl = rememberBaseUrl()
    var confirmDelete by remember { mutableStateOf(false) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Книга") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(
                            Icons.AutoMirrored.Filled.ArrowBack,
                            contentDescription = "Назад",
                        )
                    }
                },
            )
        },
    ) { padding ->
        when {
            state.loading -> {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding),
                    contentAlignment = Alignment.Center,
                ) {
                    CircularProgressIndicator()
                }
            }

            state.book == null -> {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(padding)
                        .padding(16.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text("Книга не найдена", color = MaterialTheme.colorScheme.error)
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = vm::refresh) { Text("Повторить") }
                    }
                }
            }

            else -> {
                BookDetailContent(
                    state = state,
                    baseUrl = baseUrl,
                    padding = padding,
                    onPlay = { onPlay(state.book!!.id) },
                    onDelete = { confirmDelete = true },
                )
            }
        }
    }

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Удалить с устройства?") },
            text = { Text("Локальные файлы книги будут удалены. Прогресс воспроизведения будет сброшен.") },
            confirmButton = {
                TextButton(
                    onClick = {
                        confirmDelete = false
                        vm.deleteFromDevice()
                    },
                ) {
                    Text("Удалить", color = MaterialTheme.colorScheme.error)
                }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = false }) { Text("Отмена") }
            },
        )
    }
}

@Composable
private fun BookDetailContent(
    state: BookDetailUiState,
    baseUrl: String?,
    padding: PaddingValues,
    onPlay: () -> Unit,
    onDelete: () -> Unit,
) {
    val book = state.book!!
    val online = state.isServerOnline
    val chaptersReady = state.chapters.count { it.localStatus == "ready" }
    val chaptersCount = maxOf(state.chapters.size, book.chaptersReady)

    LazyColumn(
        modifier = Modifier
            .padding(padding)
            .fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        // ---------- Шапка: обложка + метаданные ----------
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                AsyncImage(
                    model = book.coverUrl?.let { url ->
                        if (baseUrl != null) absoluteUrl(baseUrl, url) else url
                    },
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .width(120.dp)
                        .aspectRatio(1f)
                        .clip(RoundedCornerShape(8.dp)),
                )
                Spacer(Modifier.width(16.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(book.title, style = MaterialTheme.typography.titleLarge)
                    if (book.ordinal > 0) {
                        Text(
                            "Том ${book.ordinal}",
                            style = MaterialTheme.typography.titleMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    val meta = buildList {
                        book.totalDurationSec?.let { add(formatSeconds(it)) }
                        book.sizeBytes?.let { add(formatBytes(it)) }
                        if (chaptersCount > 0) add("Глав готово: $chaptersReady/$chaptersCount")
                    }.joinToString(" · ")
                    if (meta.isNotEmpty()) {
                        Text(
                            meta,
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }

        // ---------- Индикатор доступности сервера ----------
        item {
            ServerStatusRow(online = online)
        }

        // ---------- Оффлайн-подсказка (книга в очереди, сервера нет) ----------
        if (state.queued && !online) {
            item {
                Surface(
                    color = MaterialTheme.colorScheme.errorContainer,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(
                        "Сервер не доступен, загрузка будет выполнена при появлении подключения",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        modifier = Modifier.padding(10.dp),
                    )
                }
            }
        }

        // ---------- Статус загрузки на устройстве ----------
        item {
            Card(
                modifier = Modifier.fillMaxWidth(),
                elevation = CardDefaults.cardElevation(defaultElevation = 1.dp),
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Icon(
                            painter = painterResource(R.drawable.ic_download),
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.primary,
                            modifier = Modifier.size(18.dp),
                        )
                        Spacer(Modifier.width(8.dp))
                        Text(
                            "На устройстве: ${deviceStatusLabel(state.deviceStatus)}",
                            style = MaterialTheme.typography.titleSmall,
                        )
                    }
                    if (state.deviceStatus == "downloading" && chaptersCount > 0) {
                        Spacer(Modifier.height(6.dp))
                        LinearProgressIndicator(
                            progress = { chaptersReady.toFloat() / chaptersCount },
                            modifier = Modifier
                                .fillMaxWidth()
                                .clip(RoundedCornerShape(0.dp)),
                            strokeCap = StrokeCap.Butt,
                            // Убираем «квадрат-стоп» в конце трека (артефакт M3).
                            gapSize = 0.dp,
                            drawStopIndicator = {},
                        )
                        Text(
                            "Готово $chaptersReady из $chaptersCount глав",
                            style = MaterialTheme.typography.bodySmall,
                            modifier = Modifier.padding(top = 4.dp),
                        )
                    }
                }
            }
        }

        // ---------- Кнопки действий ----------
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                Button(onClick = onPlay) {
                    Icon(Icons.Default.PlayArrow, contentDescription = null)
                    Spacer(Modifier.size(4.dp))
                    Text("Слушать")
                }
            }
        }

        // ---------- Загрузка на сервере (торрент) ----------
        state.serverDownload?.let { dl ->
            if (dl.status == BookStatus.downloading || dl.status == BookStatus.partial) {
                item {
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Text(
                                "Загрузка на сервере: ${dl.chapters_ready}/${dl.chapters_total} глав",
                                style = MaterialTheme.typography.titleSmall,
                            )
                            LinearProgressIndicator(
                                progress = { dl.progress },
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .padding(vertical = 8.dp)
                                    .clip(RoundedCornerShape(0.dp)),
                                strokeCap = StrokeCap.Butt,
                                // Убираем «квадрат-стоп» в конце трека (артефакт M3).
                                gapSize = 0.dp,
                                drawStopIndicator = {},
                            )
                            Text(
                                "${formatBytes(dl.downloaded_bytes)} / ${formatBytes(dl.total_bytes)}",
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                }
            }
        }

        // ---------- Прогресс воспроизведения ----------
        state.progress?.let { saved ->
            if (saved.positionMs > 0) {
                item {
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(modifier = Modifier.padding(12.dp)) {
                            Text("Продолжить с", style = MaterialTheme.typography.titleSmall)
                            Text(
                                "Глава ${saved.chapterOrdinal}, ${formatDurationMs(saved.positionMs)}",
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                }
            }
        }

        // ---------- Удаление с устройства ----------
        item {
            TextButton(onClick = onDelete) {
                Icon(Icons.Default.Delete, contentDescription = null)
                Spacer(Modifier.size(4.dp))
                Text("Удалить с устройства", color = MaterialTheme.colorScheme.error)
            }
        }
    }
}

@Composable
private fun ServerStatusRow(online: Boolean) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Surface(
            shape = CircleShape,
            color = if (online) {
                serverOnlineColor()
            } else {
                MaterialTheme.colorScheme.error
            },
            modifier = Modifier.size(10.dp),
        ) {}
        Spacer(Modifier.width(8.dp))
        Text(
            if (online) "Сервер доступен" else "Сервер оффлайн",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

private fun deviceStatusLabel(status: String): String = when (status) {
    "ready" -> "скачана"
    "downloading" -> "скачивается"
    "partial" -> "частично"
    else -> "не скачана"
}