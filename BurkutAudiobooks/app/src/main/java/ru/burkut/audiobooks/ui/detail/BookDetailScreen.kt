@file:androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)

package ru.burkut.audiobooks.ui.detail

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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
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
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import ru.burkut.audiobooks.R
import ru.burkut.audiobooks.data.api.BookDetailOut
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.api.ChapterOut
import ru.burkut.audiobooks.data.api.ChapterProgressOut
import ru.burkut.audiobooks.data.api.ChapterStatus
import ru.burkut.audiobooks.player.absoluteUrl
import ru.burkut.audiobooks.player.chapterCacheProgress
import ru.burkut.audiobooks.player.isChapterCached
import ru.burkut.audiobooks.player.isChapterDownloading
import ru.burkut.audiobooks.ui.appInstance
import ru.burkut.audiobooks.ui.util.formatBytes
import ru.burkut.audiobooks.ui.util.formatDurationMs
import ru.burkut.audiobooks.ui.util.formatSeconds
import ru.burkut.audiobooks.ui.util.formatSpeed
import ru.burkut.audiobooks.ui.util.label
import ru.burkut.audiobooks.ui.util.rememberBaseUrl
import ru.burkut.audiobooks.ui.viewModelFactoryOf

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun BookDetailScreen(
    bookId: Int,
    onBack: () -> Unit,
    onPlay: (Int) -> Unit,
    onPlayChapter: (Int, Int) -> Unit,
    vm: BookDetailViewModel = viewModel(
        factory = viewModelFactoryOf { BookDetailViewModel(appInstance(), bookId) }
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
                    Modifier
                        .fillMaxSize()
                        .padding(padding),
                    contentAlignment = Alignment.Center,
                ) {
                    CircularProgressIndicator()
                }
            }

            state.error != null && state.book == null -> {
                Box(
                    Modifier
                        .fillMaxSize()
                        .padding(padding)
                        .padding(16.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Text(state.error!!, color = MaterialTheme.colorScheme.error)
                        Spacer(Modifier.height(8.dp))
                        Button(onClick = vm::refresh) { Text("Повторить") }
                    }
                }
            }

            state.book != null -> {
                BookDetailContent(
                    state = state,
                    baseUrl = baseUrl,
                    padding = padding,
                    onPlay = { onPlay(state.book!!.id) },
                    onPlayChapter = { chapterId -> onPlayChapter(state.book!!.id, chapterId) },
                    onStartDownload = vm::startDownload,
                    onStopDownload = vm::stopDownload,
                    onDelete = { confirmDelete = true },
                    onDownloadToCache = vm::downloadToCache,
                    onRemoveFromCache = vm::removeFromCache,
                )
            }
        }
    }

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text("Удалить книгу?") },
            text = { Text("Файлы будут удалены с сервера и из кэша устройства.") },
            confirmButton = {
                TextButton(
                    onClick = {
                        confirmDelete = false
                        vm.deleteBook(onDeleted = onBack)
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
    state: DetailState,
    baseUrl: String?,
    padding: PaddingValues,
    onPlay: () -> Unit,
    onPlayChapter: (Int) -> Unit,
    onStartDownload: () -> Unit,
    onStopDownload: () -> Unit,
    onDelete: () -> Unit,
    onDownloadToCache: () -> Unit,
    onRemoveFromCache: () -> Unit,
) {
    val book: BookDetailOut = state.book!!
    val download = state.download
    val cache = state.cache
    val progress = state.progress
    val cachedCount = book.chapters.count {
        isChapterCached(cache[cacheKey(book.id, it.id)])
    }

    LazyColumn(
        modifier = Modifier.padding(padding),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        // ---------- Шапка: обложка + метаданные ----------
        item {
            Row(verticalAlignment = Alignment.CenterVertically) {
                AsyncImage(
                    model = if (book.cover_url != null && baseUrl != null) {
                        absoluteUrl(baseUrl, book.cover_url)
                    } else null,
                    contentDescription = null,
                    contentScale = ContentScale.Crop,
                    modifier = Modifier
                        .size(140.dp)
                        .clip(RoundedCornerShape(8.dp)),
                )
                Spacer(Modifier.width(16.dp))
                Column(modifier = Modifier.weight(1f)) {
                    Text(book.title, style = MaterialTheme.typography.titleLarge)
                    book.author?.let { Text(it, style = MaterialTheme.typography.titleMedium) }
                    book.narrator?.let {
                        Text("Читает: $it", style = MaterialTheme.typography.bodyMedium)
                    }
                    book.year?.let { Text("Год: $it", style = MaterialTheme.typography.bodyMedium) }
                    book.series?.let {
                        Text("Серия: ${it.title}", style = MaterialTheme.typography.bodyMedium)
                    }
                    Text("Статус: ${book.status.label()}")
                    if (book.chapters.isNotEmpty()) {
                        Text("Глав: ${book.chapters_ready}/${book.chapters_count}")
                    }
                }
            }
        }

        // ---------- Описание ----------
        if (!book.description.isNullOrBlank()) {
            item {
                Text(book.description, style = MaterialTheme.typography.bodyMedium)
            }
        }

        // ---------- Кнопки: Слушать (всегда активна) + Удалить ----------
        item {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                // Кнопка НЕ гейтится по chapters_ready / chapters.size.
                // Список глав может быть пустым до парсинга торрент-метаданных —
                // это нормальное состояние. PlayerViewModel сам дождётся глав
                // и запустит воспроизведение.
                Button(onClick = onPlay) {
                    Icon(Icons.Default.PlayArrow, contentDescription = null)
                    Spacer(Modifier.size(4.dp))
                    Text("Слушать")
                }
                TextButton(onClick = onDelete) {
                    Icon(Icons.Default.Delete, contentDescription = null)
                    Spacer(Modifier.size(4.dp))
                    Text("Удалить")
                }
            }

            // Подсказка — не блокирует нажатие, а объясняет, что произойдёт.
            val hint = when {
                book.status == BookStatus.announced ->
                    "Сервер начнёт загрузку при нажатии"
                book.status == BookStatus.downloading && book.chapters.isEmpty() ->
                    "Сервер разбирает торрент — это может занять минуту"
                book.status == BookStatus.partial && book.chapters.isEmpty() ->
                    "Сервер разбирает торрент — это может занять минуту"
                book.chapters_ready == 0 && book.chapters.isNotEmpty() ->
                    "Первые главы будут готовы по мере загрузки"
                book.status == BookStatus.error ->
                    "Сервер сообщил об ошибке загрузки"
                else -> null
            }
            hint?.let {
                Spacer(Modifier.height(4.dp))
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }

            progress?.let { saved ->
                val chapter = book.chapters.firstOrNull { it.id == saved.chapterId }
                if (chapter != null) {
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "Продолжить: глава ${chapter.ordinal}, ${formatDurationMs(saved.positionMs)}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }

        // ---------- Кэш устройства ----------
        item {
            Text(
                "Кэш устройства ($cachedCount/${book.chapters.size} глав)",
                style = MaterialTheme.typography.titleMedium,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = onDownloadToCache) {
                    Icon(
                        painter = painterResource(R.drawable.ic_download),
                        contentDescription = null,
                    )
                    Spacer(Modifier.size(4.dp))
                    Text("Докачать все главы")
                }
                TextButton(onClick = onRemoveFromCache) {
                    Text("Очистить кэш")
                }
            }
        }

        // ---------- Прогресс загрузки на сервере ----------
        if (download != null &&
            (download.status == BookStatus.downloading || download.status == BookStatus.partial)
        ) {
            item {
                Card(modifier = Modifier.fillMaxWidth()) {
                    Column(modifier = Modifier.padding(12.dp)) {
                        Text("Загрузка на сервере: ${download.chapters_ready}/${download.chapters_total} глав")
                        LinearProgressIndicator(
                            progress = { download.progress },
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(vertical = 8.dp),
                        )
                        Text(
                            "${formatBytes(download.downloaded_bytes)} / ${formatBytes(download.total_bytes)}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Text(
                            "Скорость: ${formatSpeed(download.speed_bps)}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                        download.eta_sec?.let {
                            Text("ETA: ${it} с", style = MaterialTheme.typography.bodySmall)
                        }
                        Spacer(Modifier.height(8.dp))
                        TextButton(onClick = onStopDownload) { Text("Остановить") }
                    }
                }
            }
        }

        // ---------- Отдельная кнопка «Скачать без воспроизведения» ----------
        if (book.status == BookStatus.announced) {
            item {
                TextButton(onClick = onStartDownload) {
                    Icon(
                        painter = painterResource(R.drawable.ic_download),
                        contentDescription = null,
                    )
                    Spacer(Modifier.size(4.dp))
                    Text("Подготовить книгу заранее")
                }
            }
        }

        // ---------- Список глав ----------
        if (book.chapters.isNotEmpty()) {
            item {
                Text("Главы", style = MaterialTheme.typography.titleMedium)
            }
            items(book.chapters, key = { it.id }) { chapter ->
                ChapterRow(
                    chapter = chapter,
                    serverProgress = download?.chapters?.find { it.id == chapter.id },
                    cacheDownload = cache[cacheKey(book.id, chapter.id)],
                    isCurrent = progress?.chapterId == chapter.id,
                    onPlay = { onPlayChapter(chapter.id) },
                )
            }
        } else {
            item {
                Text(
                    "Список глав появится, когда сервер разберёт торрент",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

private fun cacheKey(bookId: Int, chapterId: Int): String = "$bookId:$chapterId"

@Composable
private fun ChapterRow(
    chapter: ChapterOut,
    serverProgress: ChapterProgressOut?,
    cacheDownload: androidx.media3.exoplayer.offline.Download?,
    isCurrent: Boolean,
    onPlay: () -> Unit,
) {
    val cached = isChapterCached(cacheDownload)
    val cacheProgress = chapterCacheProgress(cacheDownload)
    val downloading = isChapterDownloading(cacheDownload)

    Card(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable(onClick = onPlay)
                .padding(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    "Глава ${chapter.ordinal}${chapter.title?.let { " — $it" }.orEmpty()}",
                    style = MaterialTheme.typography.bodyLarge,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        formatSeconds(chapter.duration_sec),
                        style = MaterialTheme.typography.bodySmall,
                    )
                    if (serverProgress != null && serverProgress.status != ChapterStatus.ready) {
                        Text(
                            " · сервер: ${serverProgress.status.label()}",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    if (isCurrent) {
                        Text(" · продолжить", style = MaterialTheme.typography.bodySmall)
                    }
                }
                if (downloading) {
                    LinearProgressIndicator(
                        progress = { cacheProgress },
                        modifier = Modifier
                            .fillMaxWidth()
                            .padding(top = 6.dp),
                    )
                    Text(
                        "Кэш: ${(cacheProgress * 100).toInt()}%",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
            Spacer(Modifier.width(8.dp))
            if (cached) {
                Icon(
                    painter = painterResource(R.drawable.ic_check_circle),
                    contentDescription = "В кэше",
                    tint = MaterialTheme.colorScheme.secondary,
                )
            } else if (chapter.status == ChapterStatus.ready) {
                Icon(
                    Icons.Default.PlayArrow,
                    contentDescription = "Играть",
                    tint = MaterialTheme.colorScheme.onSurface.copy(alpha = 0.38f),
                )
            }
        }
    }
}