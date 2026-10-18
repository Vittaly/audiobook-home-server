package ru.burkut.audiobooks.ui.player

import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectDragGestures
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.offset
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
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import kotlin.math.roundToInt
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

    // Вход в окно плеера = книга в «быстрой» очереди; выход — её недокачанные
    // главы уходят в медленную очередь с экспоненциальной задержкой.
    DisposableEffect(Unit) {
        vm.onPlayerScreenOpened()
        onDispose { vm.onPlayerScreenClosed() }
    }

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
                        .height(6.dp)
                        .clip(RoundedCornerShape(0.dp)),
                    strokeCap = StrokeCap.Butt,
                    // Убираем «квадрат-стоп» в конце трека (артефакт M3).
                    gapSize = 0.dp,
                    drawStopIndicator = {},
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
                SquareSlider(
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

/**
 * Слайдер перемотки текущей главы в «квадратной» стилистике (без скруглений):
 * прямой трек, квадратный заполненный прогресс и квадратный бегунок.
 * Значение нормировано в диапазон 0..1.
 */
@Composable
private fun SquareSlider(
    value: Float,
    onValueChange: (Float) -> Unit,
    enabled: Boolean,
    modifier: Modifier = Modifier,
) {
    val fraction = value.coerceIn(0f, 1f)
    var widthPx by remember { mutableStateOf(1) }
    val density = LocalDensity.current
    val trackHeight = 6.dp
    val thumbSize = 14.dp
    val thumbSizePx = with(density) { thumbSize.toPx() }
    val trackHeightPx = with(density) { trackHeight.toPx() }
    val accent = MaterialTheme.colorScheme.primary
    val trackColor = MaterialTheme.colorScheme.onSurfaceVariant

    fun update(x: Float) {
        val travel = (widthPx - thumbSizePx).coerceAtLeast(1f)
        onValueChange(((x - thumbSizePx / 2f) / travel).coerceIn(0f, 1f))
    }

    Box(
        modifier = modifier
            .fillMaxWidth()
            .height(thumbSize)
            .onSizeChanged { widthPx = it.width }
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                detectTapGestures { offset -> update(offset.x) }
            }
            .pointerInput(enabled) {
                if (!enabled) return@pointerInput
                detectDragGestures { change, _ ->
                    change.consume()
                    update(change.position.x)
                }
            },
        contentAlignment = Alignment.CenterStart,
    ) {
        // Прямой трек (без скруглений).
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .height(trackHeight)
                .background(if (enabled) trackColor.copy(alpha = 0.35f) else trackColor.copy(alpha = 0.2f)),
        )
        // Заполненная часть — тоже квадратная.
        Box(
            modifier = Modifier
                .fillMaxWidth(fraction)
                .height(trackHeight)
                .background(if (enabled) accent else trackColor.copy(alpha = 0.4f)),
        )
        // Квадратный бегунок.
        Box(
            modifier = Modifier
                .offset {
                    val travel = (widthPx - thumbSizePx).coerceAtLeast(0f)
                    IntOffset((fraction * travel).roundToInt(), 0)
                }
                .size(thumbSize)
                .background(if (enabled) accent else trackColor.copy(alpha = 0.4f)),
        )
    }
}