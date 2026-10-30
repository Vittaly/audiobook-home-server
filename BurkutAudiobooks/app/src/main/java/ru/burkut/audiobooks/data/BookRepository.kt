package ru.burkut.audiobooks.data

import ru.burkut.audiobooks.data.api.ApiProvider
import ru.burkut.audiobooks.data.api.BookDetailOut
import ru.burkut.audiobooks.data.api.BookOut
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.CatalogDao
import ru.burkut.audiobooks.data.db.ChapterEntity
import ru.burkut.audiobooks.data.db.PlaybackDao
import ru.burkut.audiobooks.data.db.ProgressEntity
import ru.burkut.audiobooks.data.db.ReleaseEntity

/**
 * Cache-first источник каталога.
 *
 * Каталог (книги + главы) читается из Room — работает оффлайн.
 * Когда сервер доступен, вызывается [syncBooks], который пишет снапшот в БД.
 */
class BookRepository(
    private val apiProvider: ApiProvider,
    private val playbackDao: PlaybackDao,
    private val catalogDao: CatalogDao,
) {

    // ---------- Доступность сервера ----------

    suspend fun health(): Boolean = try {
        apiProvider.get().health().status == "ok"
    } catch (e: Exception) {
        false
    }

    suspend fun probeHealth(): Boolean = health()

    // ---------- Каталог: книги ----------

    /** Полный снапшот каталога книг с сервера (пишется в кэш). */
    suspend fun syncBooks() {
        val resp = apiProvider.get().listBooks(
            limit = 200,
            offset = 0,
            sort = "added_at",
            order = "desc",
        )
        val items = resp.items
        catalogDao.clearBooks()
        catalogDao.clearChapters()
        for (book in items) {
            catalogDao.upsertBook(book.toEntity())
        }
    }

    /** Наблюдатель всех книг каталога. */
    fun observeAllBooks() = catalogDao.observeAllBooks()

    suspend fun getBookEntity(id: String): BookEntity? = catalogDao.getBook(id)

    /** Все книги кэша (для разовой догрузки недостающих обложек). */
    suspend fun getAllBooks(): List<BookEntity> = catalogDao.getAllBooks()

    suspend fun getChapters(bookHash: String): List<ChapterEntity> = catalogDao.getChapters(bookHash)

    fun observeChapters(bookHash: String) = catalogDao.observeChapters(bookHash)

    /** Все тома раздачи (нужно для Album flow — next volume). */
    suspend fun getBooksByRelease(releaseId: Int): List<BookEntity> =
        catalogDao.getBooksByRelease(releaseId)

    /** Тома раздачи, исключая текущую книгу (для перехода к следующему тому). */
    suspend fun getBooksByRelease(releaseId: Int, excludeBookId: String): List<BookEntity> =
        getBooksByRelease(releaseId).filterNot { it.id == excludeBookId }

    suspend fun getBookByOrdinal(releaseId: Int, ordinal: Int): BookEntity? =
        catalogDao.getBookByOrdinal(releaseId, ordinal)

    suspend fun getBookDetail(bookHash: String, online: Boolean): BookDetailOut? {
        if (!online) return null
        return try {
            apiProvider.get().getBook(bookHash)
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Лёгкая проверка: финальная ли обложка книги.
     *
     * Делает HEAD /books/{hash}/cover и читает заголовок X-Cover-Temp: пока он
     * "true", сервер отдаёт временную обложку, финальной ещё нет.
     * Возвращает:
     *   true  — обложка временная (нужно продолжать опрос);
     *   false — обложка финальная (опрос можно остановить);
     *   null  — проверить не удалось (сервер недоступен/ошибка).
     */
    suspend fun coverIsTemp(bookHash: String): Boolean? = try {
        val resp = apiProvider.get().headCover(bookHash)
        resp.headers()["X-Cover-Temp"]?.equals("true", ignoreCase = true)
            ?: false
    } catch (e: Exception) {
        null
    }

    // ---------- Локальное состояние загрузки главы ----------

    suspend fun updateChapterLocal(chapterId: Int, status: String, downloaded: Long) {
        catalogDao.updateChapterLocal(chapterId, status, downloaded)
    }

    /** Сохранить реальную длительность главы, измеренную по аудиофайлу на устройстве. */
    suspend fun setChapterLocalDuration(chapterId: Int, durationSec: Int?) {
        catalogDao.setChapterLocalDuration(chapterId, durationSec)
    }

    suspend fun updateBookLocal(bookHash: String, status: String, downloaded: Long, total: Long) {
        catalogDao.updateBookLocal(bookHash, status, downloaded, total)
    }

    // ---------- Серверные операции загрузки ----------

    suspend fun startDownload(bookHash: String) = apiProvider.get().startDownload(bookHash)

    /** Старт загрузки всей раздачи без знания книг. */
    suspend fun startReleaseDownload(releaseId: Int) =
        apiProvider.get().startReleaseDownload(releaseId)

    suspend fun stopDownload(bookHash: String, deleteFiles: Boolean = false) =
        apiProvider.get().stopDownload(bookHash, deleteFiles)

    suspend fun getDownloadStatus(bookHash: String) = apiProvider.get().getDownloadStatus(bookHash)

    suspend fun deleteBook(bookHash: String) = apiProvider.get().deleteBook(bookHash)

    suspend fun setPriority(bookHash: String, priority: Int) =
        apiProvider.get().setPriority(bookHash, ru.burkut.audiobooks.data.api.PriorityRequest(priority))

    // ---------- Прогресс воспроизведения ----------

    suspend fun getProgress(bookHash: String): ProgressEntity? = playbackDao.getProgress(bookHash)

    suspend fun clearProgress(bookHash: String) = playbackDao.delete(bookHash)

    suspend fun saveProgress(bookHash: String, chapterOrdinal: Int, positionMs: Long) {
        playbackDao.upsert(
            ProgressEntity(
                bookId = bookHash,
                chapterOrdinal = chapterOrdinal,
                positionMs = positionMs,
                updatedAt = System.currentTimeMillis(),
            ),
        )
    }
}

// ---------- Мапперы API -> сущности ----------

internal fun BookOut.toEntity(): BookEntity = BookEntity(
    id = id,
    releaseId = null,
    ordinal = ordinal,
    title = title,
    author = author,
    narrator = narrator,
    year = year,
    language = language,
    rubrics = rubrics.joinToString("\n"),
    status = status.name,
    chaptersCount = chapters_count,
    chaptersReady = chapters_ready,
    totalDurationSec = total_duration_sec,
    sizeBytes = size_bytes,
    coverUrl = cover_url,
    coverIsTemp = cover_is_temp,
    addedAt = added_at,
    lastAccessed = last_accessed,
    seriesId = series?.id,
    seriesTitle = series?.title,
    seriesOrder = series?.order,
    isProvisional = false,
)

internal fun ru.burkut.audiobooks.data.api.ReleaseBookDetailOut.toEntity(releaseId: Int): BookEntity =
    BookEntity(
        id = id,
        releaseId = releaseId,
        ordinal = ordinal,
        title = title,
        folder = folder,
        author = null,
        narrator = null,
        year = null,
        language = null,
        description = null,
        status = status.name,
        chaptersCount = chapters_count,
        chaptersReady = chapters_ready,
        totalDurationSec = total_duration_sec,
        sizeBytes = size_bytes,
        coverUrl = cover_url,
        coverIsTemp = cover_is_temp,
        addedAt = "",
        lastAccessed = last_accessed,
        seriesId = series?.id,
        seriesTitle = series?.title,
        seriesOrder = series?.order,
    )

internal fun ru.burkut.audiobooks.data.api.ChapterOut.toEntity(bookId: String): ChapterEntity =
    ChapterEntity(
        id = id,
        bookId = bookId,
        ordinal = ordinal,
        title = title,
        durationSec = duration_sec,
        sizeBytes = size_bytes,
        status = status.name,
        progress = progress,
    )
