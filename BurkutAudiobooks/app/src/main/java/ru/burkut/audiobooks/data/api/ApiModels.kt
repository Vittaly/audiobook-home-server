package ru.burkut.audiobooks.data.api

import com.squareup.moshi.JsonClass


enum class BookStatus {
    announced,
    downloading,
    partial,
    ready,
    deleting,
    deleted,
    error,
}


enum class ChapterStatus {
    absent,
    downloading,
    partial,
    ready,
    deleting,
    deleted,
    error,
}

@JsonClass(generateAdapter = true)
data class SeriesRef(
    val id: Int,
    val title: String,
    val order: Int? = null,
)

@JsonClass(generateAdapter = true)
data class PersonRef(
    val name: String,
    val role: String,
)

@JsonClass(generateAdapter = true)
data class ChapterOut(
    val id: Int,
    val ordinal: Int,
    val title: String? = null,
    val duration_sec: Int? = null,
    val size_bytes: Long,
    val status: ChapterStatus,
    val progress: Float,
    val stream_url: String,
)

/** Краткое представление книги (тома) внутри карточки раздачи. */
@JsonClass(generateAdapter = true)
data class ReleaseBookOut(
    val id: String,
    val ordinal: Int,
    val title: String,
    val folder: String? = null,
    val series: SeriesRef? = null,
    val status: BookStatus,
    val chapters_count: Int = 0,
    val chapters_ready: Int = 0,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
    val cover_is_temp: Boolean = false,
    val last_accessed: String? = null,
)

/** Книга (том) внутри карточки раздачи с главами (ReleaseBookOut + chapters). */
@JsonClass(generateAdapter = true)
data class ReleaseBookDetailOut(
    val id: String,
    val ordinal: Int,
    val title: String,
    val folder: String? = null,
    val series: SeriesRef? = null,
    val status: BookStatus,
    val chapters_count: Int = 0,
    val chapters_ready: Int = 0,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
    val cover_is_temp: Boolean = false,
    val last_accessed: String? = null,
    val chapters: List<ChapterOut> = emptyList(),
)

/** Карточка раздачи для списков. */
@JsonClass(generateAdapter = true)
data class ReleaseOut(
    val id: Int,
    val title: String,
    val title_raw: String? = null,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val description: String? = null,
    val rubrics: List<String> = emptyList(),
    val status: BookStatus,
    val content_type: String = "single",
    val books_count: Int = 0,
    val books_ready: Int = 0,
    val chapters_count: Int = 0,
    val chapters_ready: Int = 0,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
    val added_at: String,
    // Дата создания раздачи на трекере (может отсутствовать у старых записей/API).
    val released_at: String? = null,
    val downloaded_at: String? = null,
    val last_accessed: String? = null,
)

/** Полная карточка раздачи с персонами и книгами. */
@JsonClass(generateAdapter = true)
data class ReleaseDetailOut(
    val id: Int,
    val title: String,
    val title_raw: String? = null,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val description: String? = null,
    val rubrics: List<String> = emptyList(),
    val status: BookStatus,
    val content_type: String = "single",
    val books_count: Int = 0,
    val books_ready: Int = 0,
    val chapters_count: Int = 0,
    val chapters_ready: Int = 0,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
    val added_at: String,
    // Дата создания раздачи на трекере (может отсутствовать у старых записей/API).
    val released_at: String? = null,
    val downloaded_at: String? = null,
    val last_accessed: String? = null,
    val persons: List<PersonRef> = emptyList(),
    val books: List<ReleaseBookDetailOut> = emptyList(),
)

@JsonClass(generateAdapter = true)
data class ReleaseListResponse(
    val items: List<ReleaseOut>,
    val total: Int,
    val limit: Int,
    val offset: Int,
)

@JsonClass(generateAdapter = true)
data class BookOut(
    val id: String,
    val ordinal: Int = 1,
    val title: String,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val series: SeriesRef? = null,
    val rubrics: List<String> = emptyList(),
    val status: BookStatus,
    val chapters_count: Int = 0,
    val chapters_ready: Int = 0,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
    val cover_is_temp: Boolean = false,
    val added_at: String,
    val last_accessed: String? = null,
)

@JsonClass(generateAdapter = true)
data class BookDetailOut(
    val id: String,
    val ordinal: Int = 1,
    val title: String,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val series: SeriesRef? = null,
    val rubrics: List<String> = emptyList(),
    val status: BookStatus,
    val chapters_count: Int = 0,
    val chapters_ready: Int = 0,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
    val cover_is_temp: Boolean = false,
    val added_at: String,
    val last_accessed: String? = null,
    val title_raw: String? = null,
    val description: String? = null,
    val persons: List<PersonRef> = emptyList(),
    val chapters: List<ChapterOut> = emptyList(),
)

@JsonClass(generateAdapter = true)
data class BookListResponse(
    val items: List<BookOut>,
    val total: Int,
    val limit: Int,
    val offset: Int,
)

@JsonClass(generateAdapter = true)
data class HealthOut(
    val status: String,
    val db: String,
    val uptime_sec: Int,
    val mode: String? = null,
    val torrent_client: String? = null,
)

@JsonClass(generateAdapter = true)
data class DownloadStartedOut(
    // Идентификатор загрузки (хэш торрента у qBittorrent/Transmission).
    val client_id: String? = null,
    // Хэш книги при старте загрузки конкретной книги.
    val book_hash: String? = null,
    val status: BookStatus,
    val save_path: String,
    val message: String,
)

@JsonClass(generateAdapter = true)
data class DownloadStoppedOut(
    val book_hash: String,
    val status: BookStatus,
    val message: String,
)

@JsonClass(generateAdapter = true)
data class ChapterProgressOut(
    val id: Int,
    val ordinal: Int,
    val title: String? = null,
    val status: ChapterStatus,
    val progress: Float,
    val size_bytes: Long,
    val downloaded_bytes: Long,
)

@JsonClass(generateAdapter = true)
data class DownloadStatusOut(
    val book_hash: String,
    val status: BookStatus,
    val client_state: String? = null,
    val progress: Float,
    val total_bytes: Long,
    val downloaded_bytes: Long,
    val speed_bps: Long,
    val eta_sec: Int? = null,
    val peers: Int,
    val seeds: Int,
    val chapters_ready: Int,
    val chapters_total: Int,
    val chapters: List<ChapterProgressOut>,
)

@JsonClass(generateAdapter = true)
data class BookDeletedOut(
    val book_hash: String,
    val status: BookStatus,
    val freed_bytes: Long,
    val message: String,
)

@JsonClass(generateAdapter = true)
data class PriorityRequest(val priority: Int)

@JsonClass(generateAdapter = true)
data class PriorityOut(
    val book_hash: String,
    val priority: Int,
    val message: String,
)

@JsonClass(generateAdapter = true)
data class ErrorOut(val detail: String)
