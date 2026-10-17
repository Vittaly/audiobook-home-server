package ru.burkut.audiobooks.data.api

import okhttp3.ResponseBody
import retrofit2.http.Body
import retrofit2.http.DELETE
import retrofit2.http.GET
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface ApiService {

    @GET("health")
    suspend fun health(): HealthOut

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

    @GET("books/{book_id}")
    suspend fun getBook(@Path("book_id") bookId: Int): BookDetailOut

    @GET("books/{book_id}/cover")
    suspend fun getCover(@Path("book_id") bookId: Int): ResponseBody

    @POST("books/{book_id}/download")
    suspend fun startDownload(@Path("book_id") bookId: Int): DownloadStartedOut

    @DELETE("books/{book_id}/download")
    suspend fun stopDownload(
        @Path("book_id") bookId: Int,
        @Query("delete_files") deleteFiles: Boolean = false,
    ): DownloadStoppedOut

    @GET("books/{book_id}/status")
    suspend fun getDownloadStatus(@Path("book_id") bookId: Int): DownloadStatusOut

    @POST("books/{book_id}/priority")
    suspend fun setPriority(
        @Path("book_id") bookId: Int,
        @Body request: PriorityRequest,
    ): PriorityOut

    @DELETE("books/{book_id}")
    suspend fun deleteBook(@Path("book_id") bookId: Int): BookDeletedOut
}
