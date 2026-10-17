package ru.burkut.audiobooks.ui.release

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import coil.compose.AsyncImage
import ru.burkut.audiobooks.R
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.ui.appInstance
import ru.burkut.audiobooks.ui.util.formatBytes
import ru.burkut.audiobooks.ui.util.formatSeconds
import ru.burkut.audiobooks.ui.viewModelFactoryOfApp

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReleaseDetailScreen(
    releaseId: Int,
    onBack: () -> Unit,
    onOpenBook: (Int) -> Unit,
    onPlayRelease: (List<BookEntity>) -> Unit,
    vm: ReleaseDetailViewModel = viewModel(
        key = "release-$releaseId",
        factory = viewModelFactoryOfApp { app ->
            ReleaseDetailViewModel(app, releaseId)
        },
    ),
) {
    val state by vm.state.collectAsState()

    // Диалог/подтверждение нет — прямая постановка в очередь.
    var showOfflineHint by remember { mutableStateOf(false) }

    LaunchedEffect(state.queued) {
        if (state.queued && !state.isServerOnline) showOfflineHint = true
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Раздача") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Назад")
                    }
                },
            )
        },
    ) { padding ->
        when {
            state.loading && state.release == null -> {
                Box(
                    modifier = Modifier
                        .padding(padding)
                        .fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) {
                    CircularProgressIndicator()
                }
            }

            state.release == null -> {
                Box(
                    modifier = Modifier
                        .padding(padding)
                        .fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) {
                    Text("Раздача не найдена")
                }
            }

            else -> {
                val release = state.release!!
                LazyColumn(
                    modifier = Modifier
                        .padding(padding)
                        .fillMaxSize(),
                ) {
                    item {
                        // Карточка раздачи.
                        Column(modifier = Modifier.padding(16.dp)) {
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                AsyncImage(
                                    model = release.coverUrl,
                                    contentDescription = null,
                                    contentScale = ContentScale.Crop,
                                    modifier = Modifier
                                        .width(100.dp)
                                        .aspectRatio(1f)
                                        .clip(RoundedCornerShape(8.dp)),
                                )
                                Column(modifier = Modifier.padding(start = 12.dp)) {
                                    Text(
                                        release.title,
                                        style = MaterialTheme.typography.titleLarge,
                                    )
                                    release.author?.let {
                                        Text(
                                            it,
                                            style = MaterialTheme.typography.bodyMedium,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        )
                                    }
                                    val meta = buildList {
                                        release.year?.let { add(it.toString()) }
                                        release.narrator?.let { add("Читает: $it") }
                                    }.joinToString(" · ")
                                    if (meta.isNotEmpty()) {
                                        Text(
                                            meta,
                                            style = MaterialTheme.typography.bodySmall,
                                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                                        )
                                    }
                                }
                            }

                            release.description?.let {
                                Spacer(Modifier.height(8.dp))
                                Text(
                                    it,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }

                            if (showOfflineHint) {
                                Spacer(Modifier.height(8.dp))
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
                    }

                    item {
                        // Кнопка загрузки раздачи целиком.
                        Row(
                            modifier = Modifier
                                .fillMaxWidth()
                                .padding(horizontal = 16.dp, vertical = 4.dp),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            if (state.queued) {
                                TextButton(onClick = vm::cancelQueue) {
                                    Icon(
                                        Icons.Default.Check,
                                        contentDescription = null,
                                        modifier = Modifier.padding(end = 4.dp),
                                    )
                                    Text("В очереди — отменить")
                                }
                            } else {
                                TextButton(onClick = vm::queueDownload) {
                                    Icon(
                                        painterResource(R.drawable.ic_download),
                                        contentDescription = null,
                                        modifier = Modifier.padding(end = 4.dp),
                                    )
                                    Text("Скачать все тома")
                                }
                            }
                            TextButton(onClick = { onPlayRelease(state.books) }) {
                                Text("Слушать")
                            }
                        }
                    }

                    if (state.books.isEmpty()) {
                        item {
                            Box(
                                modifier = Modifier.fillMaxWidth().padding(32.dp),
                                contentAlignment = Alignment.Center,
                            ) {
                                Text("Нет доступных томов")
                            }
                        }
                    } else {
                        items(state.books, key = { it.id }) { book ->
                            VolumeCard(book = book, onClick = { onOpenBook(book.id) })
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun VolumeCard(
    book: BookEntity,
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
            Surface(
                shape = CircleShape,
                color = MaterialTheme.colorScheme.primaryContainer,
                modifier = Modifier
                    .width(40.dp)
                    .height(40.dp),
            ) {
                Box(contentAlignment = Alignment.Center) {
                    Text(
                        book.ordinal.toString(),
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onPrimaryContainer,
                    )
                }
            }
            Column(
                modifier = Modifier
                    .weight(1f)
                    .padding(start = 12.dp),
            ) {
                Text(book.title, style = MaterialTheme.typography.titleMedium)
                val meta = buildList {
                    book.totalDurationSec?.let { add(formatSeconds(it)) }
                    book.sizeBytes?.let { add(formatBytes(it)) }
                    book.chaptersReady.takeIf { it > 0 }?.let { add("Глав готово: $it/${book.chaptersCount}") }
                }.joinToString(" · ")
                if (meta.isNotEmpty()) {
                    Spacer(Modifier.height(2.dp))
                    Text(
                        meta,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            IconButton(onClick = onClick) {
                Icon(
                    imageVector = Icons.AutoMirrored.Filled.ArrowBack,
                    contentDescription = null,
                )
            }
        }
    }
}