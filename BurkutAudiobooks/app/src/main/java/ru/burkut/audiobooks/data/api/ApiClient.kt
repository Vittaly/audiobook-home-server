package ru.burkut.audiobooks.data.api

import com.squareup.moshi.Moshi

import okhttp3.logging.HttpLoggingInterceptor
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import ru.burkut.audiobooks.data.settings.Settings
import java.util.concurrent.TimeUnit

class ApiProvider(private val settings: Settings) {

    @Volatile
    private var baseUrl: String? = settings.baseUrlOrNull()

    @Volatile
    private var retrofit: Retrofit? = null

    fun get(): ApiService {
        val base = settings.baseUrlOrNull()
            ?: throw IllegalStateException("Сервер не настроен")
        val current = retrofit
        if (current != null && base == baseUrl) {
            return current.create(ApiService::class.java)
        }
        synchronized(this) {
            if (base != baseUrl || retrofit == null) {
                baseUrl = base
                retrofit = Retrofit.Builder()
                    .baseUrl(base)
                    .client(httpClient)
                    .addConverterFactory(MoshiConverterFactory.create(moshi))
                    .build()
            }
            return retrofit!!.create(ApiService::class.java)
        }
    }

    companion object {
        private val moshi: Moshi = Moshi.Builder()
            .build()

        private val httpClient: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .            addInterceptor(
                HttpLoggingInterceptor().apply { level = HttpLoggingInterceptor.Level.BASIC },
            )
            .build()
    }
}
