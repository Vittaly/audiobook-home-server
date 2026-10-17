package ru.burkut.audiobooks.data

import ru.burkut.audiobooks.data.api.ApiProvider
import ru.burkut.audiobooks.data.api.BookDetailOut
import ru.burkut.audiobooks.data.api.BookListResponse
import ru.burkut.audiobooks.data.api.BookStatus
import ru.burkut.audiobooks.data.api.HealthOut
import ru.burkut.audiobooks.data.api.PriorityRequest
import ru.burkut.audiobooks.data.db.PlaybackDao
import ru.burkut.audiobooks.data.db.ProgressEntity

class BookRepository(
    private val apiProvider: ApiProvider,
    private val playbackDao: PlaybackDao,
) {

    suspend fun health(): HealthOut = apiProvider.get().health()

    suspend fun listBooks(
        query: String? = null,
        rubric: String? = null,
        status: BookStatus? = null,
        author: String? = null,
        narrator: String? = null,
        seriesId: Int? = null,
        limit: Int = 50,
        offset: Int = 0,
        sort: String? = null,
        order: String? = null,
    ): BookListResponse =
        apiProvider.get().listBooks(
            query = query,
            status = status,
            rubric = rubric,
            author = author,
            narrator = narrator,
            seriesId = seriesId,
            limit = limit,
            offset = offset,
            sort = sort,
            order = order,
        )

    suspend fun getBook(bookId: Int): BookDetailOut = apiProvider.get().getBook(bookId)

    suspend fun startDownload(bookId: Int) = apiProvider.get().startDownload(bookId)

    suspend fun stopDownload(bookId: Int, deleteFiles: Boolean = false) =
        apiProvider.get().stopDownload(bookId, deleteFiles)

    suspend fun getDownloadStatus(bookId: Int) = apiProvider.get().getDownloadStatus(bookId)

    suspend fun deleteBook(bookId: Int) = apiProvider.get().deleteBook(bookId)

    suspend fun setPriority(bookId: Int, priority: Int) =
        apiProvider.get().setPriority(bookId, PriorityRequest(priority))

    suspend fun getProgress(bookId: Int): ProgressEntity? = playbackDao.getProgress(bookId)

    suspend fun clearProgress(bookId: Int) = playbackDao.delete(bookId)

    suspend fun saveProgress(bookId: Int, chapterId: Int, positionMs: Long) {
        playbackDao.upsert(
            ProgressEntity(
                bookId = bookId,
                chapterId = chapterId,
                positionMs = positionMs,
                updatedAt = System.currentTimeMillis(),
            ),
        )
    }



}
