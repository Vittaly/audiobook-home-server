package ru.burkut.audiobooks.ui.player

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import ru.burkut.audiobooks.R
import ru.burkut.audiobooks.player.PlayerViewModel
import ru.burkut.audiobooks.ui.util.formatDurationMs

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PlayerScreen(
    vm: PlayerViewModel,
    onBack: () -> Unit,
) {
    val state by vm.state.collectAsState()
    val error by vm.error.collectAsState()
    val nextVolume by vm.requestNextVolume.collectAsState()
    val snackbarHostState = remember { SnackbarHostState() }

    LaunchedEffect(error) {
        error?.let {
            snackbarHostState.showSnackbar(it)
            vm.dismissError()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Плеер") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Назад")
                    }
                },
            )
        },
        snackbarHost = { SnackbarHost(snackbarHostState) },
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .fillMaxSize()
                .padding(horizontal = 20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {

            if (state.preparing || state.isBuffering) {
                CircularProgressIndicator(
                    modifier = Modifier
                        .size(28.dp)
                        .padding(top = 8.dp),
                )
            }

            // ---------- Обложка ----------
            AsyncImage(
                model = state.artUri,
                contentDescription = null,
                contentScale = ContentScale.Crop,
                modifier = Modifier
                    .padding(top = 8.dp)
                    .fillMaxWidth(0.72f)
                    .aspectRatio(1f)
                    .clip(RoundedCornerShape(12.dp)),
            )

            Spacer(Modifier.height(16.dp))

            // ---------- Книга + имя файла (без списка глав) ----------
            Text(
                state.title ?: "Ничего не воспроизводится",
                style = MaterialTheme.typography.titleLarge,
                textAlign = TextAlign.Center,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            val fileName = state.fileName
            if (fileName != null) {
                Text(
                    fileName,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    textAlign = TextAlign.Center,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }

            // ---------- Оффлайн: книга в очереди ----------
            if (state.offlineQueued) {
                Surface(
                    color = MaterialTheme.colorScheme.errorContainer,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 12.dp),
                ) {
                    Text(
                        "Сервер не доступен, загрузка будет выполнена при появлении подключения",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onErrorContainer,
                        modifier = Modifier.padding(10.dp),
                    )
                }
            }

            Spacer(Modifier.weight(1f))

            // ================================================================
            // ВЕРХНИЙ БАР: длительность всей книги
            // (пересчитывается по мере готовности глав; скролл/слайдер не нужен)
            // ================================================================
            if (state.hasMedia) {
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        "Книга",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        "${formatDurationMs(state.bookPositionMs)} / ${formatDurationMs(state.bookDurationMs)}",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                LinearProgressIndicator(
                    progress = {
                        if (state.bookDurationMs > 0) {
                            (state.bookPositionMs.toFloat() / state.bookDurationMs)
                                .coerceIn(0f, 1f)
                        } else 0f
                    },
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(6.dp),
                    strokeCap = StrokeCap.Butt,
                )

                Spacer(Modifier.height(20.dp))

                // ================================================================
                // НИЖНИЙ БАР: текущая глава, с перемоткой
                // ================================================================
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        "Глава",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Text(
                        "${formatDurationMs(state.positionMs)} / ${formatDurationMs(state.durationMs)}",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Slider(
                    value = if (state.durationMs > 0) {
                        (state.positionMs.toFloat() / state.durationMs).coerceIn(0f, 1f)
                    } else 0f,
                    onValueChange = { vm.seekTo((it * state.durationMs).toLong()) },
                    enabled = state.durationMs > 0 && state.hasMedia,
                    modifier = Modifier.fillMaxWidth(),
                )
            }

            Spacer(Modifier.weight(1f))

            // ---------- Управление ----------
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(16.dp),
            ) {
                IconButton(onClick = vm::previous) {
                    Icon(
                        painter = painterResource(R.drawable.ic_skip_previous),
                        contentDescription = "Предыдущая",
                        modifier = Modifier.size(36.dp),
                    )
                }
                IconButton(onClick = vm::togglePlayPause) {
                    if (state.isPlaying) {
                        Icon(
                            painter = painterResource(R.drawable.ic_pause),
                            contentDescription = "Пауза",
                            modifier = Modifier.size(56.dp),
                        )
                    } else {
                        Icon(
                            imageVector = Icons.Default.PlayArrow,
                            contentDescription = "Играть",
                            modifier = Modifier.size(56.dp),
                        )
                    }
                }
                IconButton(onClick = vm::next) {
                    Icon(
                        painter = painterResource(R.drawable.ic_skip_next),
                        contentDescription = "Следующая",
                        modifier = Modifier.size(36.dp),
                    )
                }
            }

            Spacer(Modifier.height(12.dp))

            // ---------- Индикатор доступа к серверу ----------
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                modifier = Modifier.padding(bottom = 12.dp),
            ) {
                Surface(
                    shape = CircleShape,
                    color = if (state.isServerOnline) {
                        MaterialTheme.colorScheme.primary
                    } else {
                        MaterialTheme.colorScheme.error
                    },
                    modifier = Modifier.size(8.dp),
                ) {}
                Text(
                    if (state.isServerOnline) "Сервер доступен" else "Сервер оффлайн",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }

    // ---------- Запрос перехода к следующему тому ----------
    nextVolume?.let { next ->
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .padding(12.dp),
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(
                    "Закончился текущий том",
                    style = MaterialTheme.typography.titleSmall,
                )
                Text(
                    "Следующий: ${next.title}",
                    style = MaterialTheme.typography.bodyMedium,
                    modifier = Modifier.padding(top = 4.dp),
                )
                Row(
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    modifier = Modifier.padding(top = 8.dp),
                ) {
                    Button(onClick = { vm.startNextVolume(next.id) }) {
                        Text("Играть")
                    }
                    TextButton(onClick = vm::dismissNextVolume) { Text("Позже") }
                }
            }
        }
    }
}