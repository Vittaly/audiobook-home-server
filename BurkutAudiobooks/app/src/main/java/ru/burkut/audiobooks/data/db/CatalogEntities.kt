package ru.burkut.audiobooks.data.db

import androidx.room.Entity
import androidx.room.PrimaryKey

/** Карточка раздачи (кэш каталога). */
@Entity(tableName = "releases")
data class ReleaseEntity(
    @PrimaryKey val id: Int,
    val title: String,
    val titleRaw: String? = null,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val description: String? = null,
    val rubrics: String? = null,
    val status: String,
    val contentType: String = "single",
    val booksCount: Int = 0,
    val booksReady: Int = 0,
    val chaptersCount: Int = 0,
    val chaptersReady: Int = 0,
    val totalDurationSec: Int? = null,
    val sizeBytes: Long? = null,
    val coverUrl: String? = null,
    // Время индексации карточки на сервере (ISO-8601).
    val addedAt: String,
    // Дата создания раздачи на трекере (ISO-8601). Основная дата каталога:
    // по ней идёт сортировка «сначала новые» и её показываем на карточке.
    val releasedAt: String? = null,
    val downloadedAt: String? = null,
    val lastAccessed: String? = null,
    // Пометка: раздача поставлена в фоновую докачку на устройство.
    val queuedForDevice: Boolean = false,
)

/** Книга (том) внутри раздачи. */
@Entity(tableName = "books")
data class BookEntity(
    @PrimaryKey val id: Int,
    val releaseId: Int,
    val ordinal: Int,
    val title: String,
    val folder: String? = null,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val description: String? = null,
    val status: String,
    val chaptersCount: Int = 0,
    val chaptersReady: Int = 0,
    val totalDurationSec: Int? = null,
    val sizeBytes: Long? = null,
    val coverUrl: String? = null,
    val coverIsTemp: Boolean = false,
    val addedAt: String,
    val lastAccessed: String? = null,
    // Серия книги (заполняется из карточки раздачи при загрузке томов).
    val seriesId: Int? = null,
    val seriesTitle: String? = null,
    val seriesOrder: Int? = null,
    // Локальный статус загрузки главы на устройство.
    val localStatus: String = "absent", // absent | downloading | partial | ready
    val downloadedBytes: Long = 0,
    val localTotalBytes: Long = 0,
)

/** Глава книги — серверное + локальное состояние. */
@Entity(tableName = "chapters")
data class ChapterEntity(
    @PrimaryKey val id: Int,
    val bookId: Int,
    val ordinal: Int,
    val title: String? = null,
    val durationSec: Int? = null,
    val sizeBytes: Long,
    val status: String,
    val progress: Float,
    // Локальный статус: скачана ли на устройство эта глава.
    val localStatus: String = "absent", // absent | downloading | partial | ready
    val downloadedBytes: Long = 0,
    // Реальная длительность, измеренная клиентом по содержимому аудиофайла.
    // Сервер не знает длительности глав — поэтому приоритет у этого поля.
    val localDurationSec: Int? = null,
)
