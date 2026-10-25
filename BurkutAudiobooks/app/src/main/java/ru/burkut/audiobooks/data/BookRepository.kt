package ru.burkut.audiobooks.data

import ru.burkut.audiobooks.data.api.ApiProvider
import ru.burkut.audiobooks.data.api.BookDetailOut
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.api.ReleaseDetailOut
import ru.burkut.audiobooks.data.api.ReleaseOut
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.CatalogDao
import ru.burkut.audiobooks.data.db.ChapterEntity
import ru.burkut.audiobooks.data.db.PlaybackDao
import ru.burkut.audiobooks.data.db.ProgressEntity
import ru.burkut.audiobooks.data.db.ReleaseEntity

/**
 * Cache-first источник каталога.
 *
 * Каталог (раздачи + книги + главы) читается из Room — работает оффлайн.
 * Когда сервер доступен, вызывается [syncReleases] / [refreshRelease],
 * которые пишут снапшот в БД.
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

    // ---------- Каталог: раздачи ----------

    fun observeReleases() = catalogDao.observeReleases()

    /** Полный снапшот каталога раздач с сервера (пишется в кэш). */
    suspend fun syncReleases() {
        // Снапшот каталога берём сразу отсортированным по дате релиза на
        // трекере (сначала новые), чтобы локальный Room заполнялся в целевом
        // порядке и «Сначала новые» на клиенте совпадала с сервером.
        val resp = apiProvider.get().listReleases(
            limit = 200,
            offset = 0,
            sort = "released_at",
            order = "desc",
        )
        val items = resp.items
        // Заменяем карточки раздач, но книги и главы НЕ очищаем целиком:
        // список /releases не содержит вложенных книг, и полная очистка
        // теряла тома уже открытой раздачи (на экране «Слушать» — books=0).
        // Вместо этого после замены раздач удаляем только осиротевшие записи.
        val existing = catalogDao.getQueuedReleases().associateBy { it.id }
        catalogDao.clearReleases()
        for (item in items) {
            val prev = existing[item.id]
            catalogDao.upsertRelease(
                item.toEntity().copy(
                    queuedForDevice = prev?.queuedForDevice ?: false,
                ),
            )
        }
        // Чистим книги/главы, чьей раздачи/книги больше нет в каталоге.
        catalogDao.deleteOrphanBooks()
        catalogDao.deleteOrphanChapters()
    }

    suspend fun getReleaseEntity(id: Int): ReleaseEntity? = catalogDao.getRelease(id)

    /** Свежая карточка раздачи по ID книги (нужно PlayerViewModel для докачки глав). */
    suspend fun getReleaseDetailForBook(bookId: Int, online: Boolean): ReleaseDetailOut? {
        if (!online) return null
        val book = catalogDao.getBook(bookId) ?: return null
        return getReleaseDetail(book.releaseId, online)
    }

    /** Полная карточка раздачи: при доступности берём свежую с сервера и кэшируем. */
    suspend fun getReleaseDetail(id: Int, online: Boolean): ReleaseDetailOut? {
        android.util.Log.d("Repo", "getReleaseDetail id=$id online=$online")
        if (!online) return null
        val detail = try {
            apiProvider.get().getRelease(id)
        } catch (e: Exception) {
            android.util.Log.e("Repo", "getReleaseDetail HTTP error id=$id", e)
            null
        } ?: run {
            android.util.Log.w("Repo", "getReleaseDetail null id=$id")
            return null
        }
        android.util.Log.d("Repo", "getReleaseDetail ok id=$id books=${detail.books.size} status=${detail.status}")
        catalogDao.upsertRelease(detail.toEntity())
        for (book in detail.books) {
            val bookOut = book
            android.util.Log.d("Repo", "upsert book ${bookOut.id} ordinal=${bookOut.ordinal} chapters=${bookOut.chapters.size}")
            catalogDao.upsertBook(bookOut.toEntity(id))
            for (ch in bookOut.chapters) {
                // upsertChapter использует OnConflictStrategy.REPLACE, который удаляет
                // строку и вставляет новую — локальные поля (localDurationSec,
                // localStatus, downloadedBytes) были бы затёрты на каждом поллинге
                // сервера. Подмешиваем их из уже сохранённой записи.
                val prev = catalogDao.getChapter(ch.id)
                val entity = ch.toEntity(bookOut.id).copy(
                    localStatus = prev?.localStatus ?: "absent",
                    downloadedBytes = prev?.downloadedBytes ?: 0,
                    localDurationSec = prev?.localDurationSec,
                )
                catalogDao.upsertChapter(entity)
            }
        }
        return detail
    }

    // ---------- Каталог: книги ----------

    fun observeBooks(releaseId: Int) = catalogDao.observeBooks(releaseId)

    /** Все книги кэша — каталог использует их для фильтрации раздач по серии. */
    fun observeAllBooks() = catalogDao.observeAllBooks()

    suspend fun getBookEntity(id: Int): BookEntity? = catalogDao.getBook(id)

    /** Все книги кэша (для разовой догрузки недостающих обложек). */
    suspend fun getAllBooks(): List<BookEntity> = catalogDao.getAllBooks()

    suspend fun getChapters(bookId: Int): List<ChapterEntity> = catalogDao.getChapters(bookId)

    fun observeChapters(bookId: Int) = catalogDao.observeChapters(bookId)

    /** Все тома раздачи. */
    suspend fun getBooksByRelease(releaseId: Int): List<BookEntity> =
        catalogDao.getBooksByRelease(releaseId)

    /** Тома раздачи, исключая текущую книгу (для перехода к следующему тому). */
    suspend fun getBooksByRelease(releaseId: Int, excludeBookId: Int): List<BookEntity> =
        getBooksByRelease(releaseId).filterNot { it.id == excludeBookId }

    suspend fun getBookByOrdinal(releaseId: Int, ordinal: Int): BookEntity? =
        catalogDao.getBookByOrdinal(releaseId, ordinal)

    suspend fun getBookDetail(bookId: Int, online: Boolean): BookDetailOut? {
        if (!online) return null
        return try {
            apiProvider.get().getBook(bookId)
        } catch (e: Exception) {
            null
        }
    }

    /**
     * Лёгкая проверка: финальная ли обложка книги.
     *
     * Делает HEAD /books/{id}/cover и читает заголовок X-Cover-Temp: пока он
     * "true", сервер отдаёт временную обложку (раздачи), финальной ещё нет.
     * Возвращает:
     *   true  — обложка временная (нужно продолжать опрос);
     *   false — обложка финальная (опрос можно остановить);
     *   null  — проверить не удалось (сервер недоступен/ошибка).
     */
    suspend fun coverIsTemp(bookId: Int): Boolean? = try {
        val resp = apiProvider.get().headCover(bookId)
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

    suspend fun updateBookLocal(bookId: Int, status: String, downloaded: Long, total: Long) {
        catalogDao.updateBookLocal(bookId, status, downloaded, total)
    }

    suspend fun getQueuedReleases(): List<ReleaseEntity> = catalogDao.getQueuedReleases()

    suspend fun setReleaseQueued(releaseId: Int, queued: Boolean) {
        catalogDao.setReleaseQueued(releaseId, queued)
    }

    // ---------- Серверные операции загрузки ----------

    suspend fun startDownload(bookId: Int) = apiProvider.get().startDownload(bookId)

    /** Старт загрузки всей раздачи без знания книг (сервер не создаёт книгу-заглушку). */
    suspend fun startReleaseDownload(releaseId: Int, bookId: Int? = null) =
        apiProvider.get().startReleaseDownload(releaseId, bookId)

    suspend fun stopDownload(bookId: Int, deleteFiles: Boolean = false) =
        apiProvider.get().stopDownload(bookId, deleteFiles)

    suspend fun getDownloadStatus(bookId: Int) = apiProvider.get().getDownloadStatus(bookId)

    suspend fun deleteBook(bookId: Int) = apiProvider.get().deleteBook(bookId)

    suspend fun setPriority(bookId: Int, priority: Int) =
        apiProvider.get().setPriority(bookId, ru.burkut.audiobooks.data.api.PriorityRequest(priority))

    // ---------- Прогресс воспроизведения ----------

    suspend fun getProgress(bookId: Int): ProgressEntity? = playbackDao.getProgress(bookId)

    suspend fun clearProgress(bookId: Int) = playbackDao.delete(bookId)

    suspend fun saveProgress(bookId: Int, chapterOrdinal: Int, positionMs: Long) {
        playbackDao.upsert(
            ProgressEntity(
                bookId = bookId,
                chapterOrdinal = chapterOrdinal,
                positionMs = positionMs,
                updatedAt = System.currentTimeMillis(),
            ),
        )
    }
}

// ---------- Мапперы API -> сущности ----------

internal fun ReleaseOut.toEntity(): ReleaseEntity = ReleaseEntity(
    id = id,
    title = title,
    titleRaw = title_raw,
    author = author,
    narrator = narrator,
    year = year,
    language = language,
    description = description,
    rubrics = rubrics.joinToString("\n"),
    status = status.name,
    contentType = content_type,
    booksCount = books_count,
    booksReady = books_ready,
    chaptersCount = chapters_count,
    chaptersReady = chapters_ready,
    totalDurationSec = total_duration_sec,
    sizeBytes = size_bytes,
    coverUrl = cover_url,
    addedAt = added_at,
    releasedAt = released_at ?: added_at,
    downloadedAt = downloaded_at,
    lastAccessed = last_accessed,
)

internal fun ReleaseDetailOut.toEntity(): ReleaseEntity = ReleaseEntity(
    id = id,
    title = title,
    titleRaw = title_raw,
    author = author,
    narrator = narrator,
    year = year,
    language = language,
    description = description,
    rubrics = rubrics.joinToString("\n"),
    status = status.name,
    contentType = content_type,
    booksCount = books_count,
    booksReady = books_ready,
    chaptersCount = chapters_count,
    chaptersReady = chapters_ready,
    totalDurationSec = total_duration_sec,
    sizeBytes = size_bytes,
    coverUrl = cover_url,
    addedAt = added_at,
    releasedAt = released_at ?: added_at,
    downloadedAt = downloaded_at,
    lastAccessed = last_accessed,
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

internal fun ru.burkut.audiobooks.data.api.ChapterOut.toEntity(bookId: Int): ChapterEntity =
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
