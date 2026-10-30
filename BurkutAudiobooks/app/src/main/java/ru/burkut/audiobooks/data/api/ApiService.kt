package ru.burkut.audiobooks.data.api

import okhttp3.ResponseBody
import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.HEAD
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface ApiService {

    @GET("health")
    suspend fun health(): HealthOut

    @GET("releases")
    suspend fun listReleases(
        @Query("query") query: String? = null,
        @Query("status") status: BookStatus? = null,
        @Query("rubric") rubric: String? = null,
        @Query("author") author: String? = null,
        @Query("narrator") narrator: String? = null,
        @Query("series_id") seriesId: Int? = null,
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0,
        @Query("sort") sort: String? = null,
        @Query("order") order: String? = null,
    ): ReleaseListResponse

    @GET("releases/{release_id}")
    suspend fun getRelease(@Path("release_id") releaseId: Int): ReleaseDetailOut

    @GET("releases/{release_id}/cover")
    suspend fun getReleaseCover(@Path("release_id") releaseId: Int): ResponseBody

    @GET("books")
    suspend fun listBooks(
        @Query("query") query: String? = null,
        @Query("status") status: BookStatus? = null,
        @Query("rubric") rubric: String? = null,
        @Query("author") author: String? = null,
        @Query("narrator") narrator: String? = null,
        @Query("series_id") seriesId: Int? = null,
        @Query("limit") limit: Int = 50,
        @Query("offset") offset: Int = 0,
        @Query("sort") sort: String? = null,
        @Query("order") order: String? = null,
    ): BookListResponse

    @GET("books/{book_hash}")
    suspend fun getBook(@Path("book_hash") bookHash: String): BookDetailOut

    @GET("books/{book_hash}/cover")
    suspend fun getCover(@Path("book_hash") bookHash: String): ResponseBody

    /**
     * Лёгкая проверка обложки: HEAD /books/{hash}/cover отдаёт заголовки без
     * тела. Заголовок X-Cover-Temp == "true" означает, что обложка ещё
     * временная (можно её получить позже), его отсутствие — финал.
     */
    @HEAD("books/{book_hash}/cover")
    suspend fun headCover(
        @Path("book_hash") bookHash: String,
    ): Response<ResponseBody>

    @POST("books/{book_hash}/download")
    suspend fun startDownload(@Path("book_hash") bookHash: String): DownloadStartedOut

    @POST("releases/{release_id}/download")
    suspend fun startReleaseDownload(
        @Path("release_id") releaseId: Int,
    ): DownloadStartedOut

    @DELETE("books/{book_hash}/download")
    suspend fun stopDownload(
        @Path("book_hash") bookHash: String,
        @Query("delete_files") deleteFiles: Boolean = false,
    ): DownloadStoppedOut

    @GET("books/{book_hash}/status")
    suspend fun getDownloadStatus(@Path("book_hash") bookHash: String): DownloadStatusOut

    @POST("books/{book_hash}/priority")
    suspend fun setPriority(
        @Path("book_hash") bookHash: String,
        @Body request: PriorityRequest,
    ): PriorityOut

    @DELETE("books/{book_hash}")
    suspend fun deleteBook(@Path("book_hash") bookHash: String): BookDeletedOut
}
