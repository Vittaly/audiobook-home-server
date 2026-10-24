package ru.burkut.audiobooks.ui.util

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.ui.platform.LocalContext
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.api.ChapterStatus
import java.util.Locale

@Composable
fun rememberBaseUrl(): String? {
    val context = LocalContext.current
    val settings = (context.applicationContext as App).settings
    return settings.baseUrl.collectAsState().value
}

fun formatDurationMs(ms: Long): String {
    val totalSec = (ms / 1000).toInt()
    val h = totalSec / 3600
    val m = (totalSec % 3600) / 60
    val s = totalSec % 60
    return if (h > 0) {
        String.format(Locale.US, "%d:%02d:%02d", h, m, s)
    } else {
        String.format(Locale.US, "%02d:%02d", m, s)
    }
}

fun formatSeconds(sec: Int?): String =
    if (sec == null) "—" else formatDurationMs(sec * 1000L)

/**
 * Дата релиза с трекера (ISO-8601: 'YYYY-MM-DD' или 'YYYY-MM-DDTHH:MM:SS±...')
 * → 'dd.MM.yyyy'. Если строка пустая или неожиданного формата — null.
 */
fun formatReleaseDate(iso: String?): String? {
    val datePart = iso?.substringBefore('T')?.substringBefore(' ')?.trim()
    if (datePart.isNullOrEmpty()) return null
    val parts = datePart.split('-')
    if (parts.size != 3 || parts[0].length != 4) return null
    val (year, month, day) = parts
    return "$day.$month.$year"
}

/**
 * Разобранное описание раздачи: [intro] — основной текст, [contents] — пункты
 * перечня из встроенного блока «Содержание».
 */
data class ParsedDescription(
    val intro: String,
    val contents: List<String>,
)

/**
 * Разбирает `release.description`. Сервер сохраняет «Описание» трекера целиком,
 * включая встроенный блок вида `... Содержание: 1. Первый\n2. Второй ...`.
 * Выделяем этот блок: [ParsedDescription.intro] — текст до метки, а
 * [ParsedDescription.contents] — отдельные пункты (каждый с новой строки).
 */
fun parseReleaseDescription(description: String?): ParsedDescription? {
    if (description.isNullOrBlank()) return null
    val marker = Regex("(?i)содержание\\s*:?")
    val m = marker.find(description)
        ?: return ParsedDescription(description.trim(), emptyList())
    val intro = description.substring(0, m.range.first).trim()
    val contents = description.substring(m.range.last + 1)
        .split('\n')
        .map { it.trim().trimStart('-', '·', '•').trim() }
        .filter { it.isNotEmpty() }
    return ParsedDescription(intro, contents)
}

fun formatBytes(bytes: Long): String {
    val kb = bytes / 1024.0
    return when {
        kb >= 1024 * 1024 -> String.format(Locale.US, "%.1f ГБ", kb / 1024 / 1024)
        kb >= 1024 -> String.format(Locale.US, "%.1f МБ", kb / 1024)
        else -> String.format(Locale.US, "%d КБ", bytes / 1024)
    }
}

fun formatSpeed(bps: Long): String =
    if (bps <= 0) "—" else formatBytes(bps) + "/с"

fun BookStatus.label(): String = when (this) {
    BookStatus.announced -> "Анонсирована"
    BookStatus.downloading -> "Загрузка"
    BookStatus.partial -> "Частично"
    BookStatus.ready -> "Готова"
    BookStatus.deleting -> "Удаление"
    BookStatus.deleted -> "Удалена"
    BookStatus.error -> "Ошибка"
}

fun ChapterStatus.label(): String = when (this) {
    ChapterStatus.absent -> "Нет"
    ChapterStatus.downloading -> "Качается"
    ChapterStatus.partial -> "Частично"
    ChapterStatus.ready -> "Готова"
    ChapterStatus.deleting -> "Удаление"
    ChapterStatus.deleted -> "Удалена"
    ChapterStatus.error -> "Ошибка"
}
