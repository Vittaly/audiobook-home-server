package ru.burkut.audiobooks.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

@Dao
interface CatalogDao {

    // ---------- Releases ----------

    // Базовый порядок — по дате релиза с трекера (releasedAt), от свежих
    // к старым. Даты хранятся в ISO-8601, лексикографическая сортировка
    // совпадает с хронологической. Если даты трекера нет, берём addedAt.
    // Итоговую сортировку дополнительно применяет ViewModel.
    @Query(
        "SELECT * FROM releases " +
            "ORDER BY COALESCE(releasedAt, addedAt) DESC"
    )
    fun observeReleases(): kotlinx.coroutines.flow.Flow<List<ReleaseEntity>>

    @Query("SELECT * FROM releases WHERE id = :id")
    suspend fun getRelease(id: Int): ReleaseEntity?

    @Query("SELECT * FROM releases WHERE queuedForDevice = 1")
    suspend fun getQueuedReleases(): List<ReleaseEntity>

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertRelease(entity: ReleaseEntity): Long

    @Query("UPDATE releases SET queuedForDevice = :queued WHERE id = :id")
    suspend fun setReleaseQueued(id: Int, queued: Boolean)

    @Query("UPDATE releases SET status = :status, booksReady = :booksReady, chaptersReady = :chaptersReady WHERE id = :id")
    suspend fun updateReleaseProgress(id: Int, status: String, booksReady: Int, chaptersReady: Int)

    // ---------- Books ----------

    @Query("SELECT * FROM books WHERE releaseId = :releaseId ORDER BY ordinal")
    fun observeBooks(releaseId: Int): kotlinx.coroutines.flow.Flow<List<BookEntity>>

    /** Все книги кэша — нужны каталогу для фильтрации раздач по серии. */
    @Query("SELECT * FROM books")
    fun observeAllBooks(): kotlinx.coroutines.flow.Flow<List<BookEntity>>

    @Query("SELECT * FROM books WHERE id = :id")
    suspend fun getBook(id: Int): BookEntity?

    @Query("SELECT * FROM books WHERE releaseId = :releaseId ORDER BY ordinal")
    suspend fun getBooksByRelease(releaseId: Int): List<BookEntity>

    @Query("SELECT * FROM books WHERE releaseId = :releaseId AND ordinal = :ordinal")
    suspend fun getBookByOrdinal(releaseId: Int, ordinal: Int): BookEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertBook(entity: BookEntity): Long

    @Query("UPDATE books SET localStatus = :status, downloadedBytes = :downloaded, localTotalBytes = :total WHERE id = :id")
    suspend fun updateBookLocal(id: Int, status: String, downloaded: Long, total: Long)

    // ---------- Chapters ----------

    @Query("SELECT * FROM chapters WHERE bookId = :bookId ORDER BY ordinal")
    fun observeChapters(bookId: Int): kotlinx.coroutines.flow.Flow<List<ChapterEntity>>

    @Query("SELECT * FROM chapters WHERE bookId = :bookId ORDER BY ordinal")
    suspend fun getChapters(bookId: Int): List<ChapterEntity>

    @Query("SELECT * FROM chapters WHERE id = :id")
    suspend fun getChapter(id: Int): ChapterEntity?

    @Query("SELECT * FROM chapters WHERE bookId = :bookId AND ordinal = :ordinal")
    suspend fun getChapterByOrdinal(bookId: Int, ordinal: Int): ChapterEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsertChapter(entity: ChapterEntity): Long

    @Query("UPDATE chapters SET localStatus = :status, downloadedBytes = :downloaded WHERE id = :id")
    suspend fun updateChapterLocal(id: Int, status: String, downloaded: Long)

    /** Сохранить реальную длительность главы, измеренную по аудиофайлу на устройстве. */
    @Query("UPDATE chapters SET localDurationSec = :durationSec WHERE id = :id")
    suspend fun setChapterLocalDuration(id: Int, durationSec: Int?)

    // ---------- Snapshot (полная замена каталога раздач) ----------

    @Query("DELETE FROM releases")
    suspend fun clearReleases()

    /** Удаляет только книги, чьей раздачи больше нет в каталоге (осиротевшие). */
    @Query("DELETE FROM books WHERE releaseId NOT IN (SELECT id FROM releases)")
    suspend fun deleteOrphanBooks()

    /** Удаляет только главы, чьей книги больше нет в каталоге (осиротевшие). */
    @Query("DELETE FROM chapters WHERE bookId NOT IN (SELECT id FROM books)")
    suspend fun deleteOrphanChapters()

    @Query("DELETE FROM books")
    suspend fun clearBooks()

    @Query("DELETE FROM chapters")
    suspend fun clearChapters()
}
