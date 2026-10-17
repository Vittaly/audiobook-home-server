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

@JsonClass(generateAdapter = true)
data class BookOut(
    val id: Int,
    val title: String,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val series: SeriesRef? = null,
    val rubrics: List<String> = emptyList(),
    val status: BookStatus,
    val chapters_count: Int,
    val chapters_ready: Int,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
    val added_at: String,
    val last_accessed: String? = null,
)

@JsonClass(generateAdapter = true)
data class BookDetailOut(
    val id: Int,
    val title: String,
    val author: String? = null,
    val narrator: String? = null,
    val year: Int? = null,
    val language: String? = null,
    val series: SeriesRef? = null,
    val rubrics: List<String> = emptyList(),
    val status: BookStatus,
    val chapters_count: Int,
    val chapters_ready: Int,
    val total_duration_sec: Int? = null,
    val size_bytes: Long? = null,
    val cover_url: String? = null,
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
)

@JsonClass(generateAdapter = true)
data class DownloadStartedOut(
    val book_id: Int,
    val client_id: Int? = null,
    val status: BookStatus,
    val save_path: String,
    val message: String,
)

@JsonClass(generateAdapter = true)
data class DownloadStoppedOut(
    val book_id: Int,
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
    val book_id: Int,
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
    val book_id: Int,
    val status: BookStatus,
    val freed_bytes: Long,
    val message: String,
)

@JsonClass(generateAdapter = true)
data class PriorityRequest(val priority: Int)

@JsonClass(generateAdapter = true)
data class PriorityOut(
    val book_id: Int,
    val priority: Int,
    val message: String,
)

@JsonClass(generateAdapter = true)
data class ErrorOut(val detail: String)
