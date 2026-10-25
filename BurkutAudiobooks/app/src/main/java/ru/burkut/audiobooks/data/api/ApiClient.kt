package ru.burkut.audiobooks.data.api

import com.squareup.moshi.JsonAdapter
import com.squareup.moshi.Moshi
import com.squareup.moshi.Types

import okhttp3.logging.HttpLoggingInterceptor
import okhttp3.OkHttpClient
import retrofit2.Retrofit
import retrofit2.converter.moshi.MoshiConverterFactory
import ru.burkut.audiobooks.data.settings.Settings
import java.lang.reflect.Type
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
        /**
         * Moshi со сгенерированными Moshi-адаптерами, зарегистрированными явно.
         *
         * Регистрация идёт через прямые ссылки на классы-адаптеры (JsonAdapter), а не через
         * рефлексию по имени класса (как делает встроенный поиск Moshi). Это гарантирует,
         * что R8 не выкинет адаптеры из release-сборки (иначе: "Unable to create converter").
         */
        private val moshi: Moshi = Moshi.Builder()
            .add(GeneratedAdapterFactory)
            .build()

        /** Перечисляет все сгенерированные Moshi-адаптеры для DTO приложения. */
        private object GeneratedAdapterFactory : JsonAdapter.Factory {
            override fun create(
                type: Type,
                annotations: Set<Annotation>,
                moshi: Moshi,
            ): JsonAdapter<*>? {
                if (annotations.isNotEmpty()) return null
                val raw = Types.getRawType(type)
                val adapter: JsonAdapter<*>? = when (raw) {
                    // --- Ответы списков ---
                    BookListResponse::class.java -> BookListResponseJsonAdapter(moshi)
                    ReleaseListResponse::class.java -> ReleaseListResponseJsonAdapter(moshi)
                    // --- Карточки ---
                    ReleaseOut::class.java -> ReleaseOutJsonAdapter(moshi)
                    ReleaseDetailOut::class.java -> ReleaseDetailOutJsonAdapter(moshi)
                    ReleaseBookOut::class.java -> ReleaseBookOutJsonAdapter(moshi)
                    ReleaseBookDetailOut::class.java -> ReleaseBookDetailOutJsonAdapter(moshi)
                    BookOut::class.java -> BookOutJsonAdapter(moshi)
                    BookDetailOut::class.java -> BookDetailOutJsonAdapter(moshi)
                    // --- Вложенные объекты ---
                    SeriesRef::class.java -> SeriesRefJsonAdapter(moshi)
                    PersonRef::class.java -> PersonRefJsonAdapter(moshi)
                    ChapterOut::class.java -> ChapterOutJsonAdapter(moshi)
                    ChapterProgressOut::class.java -> ChapterProgressOutJsonAdapter(moshi)
                    // --- Операции ---
                    HealthOut::class.java -> HealthOutJsonAdapter(moshi)
                    DownloadStartedOut::class.java -> DownloadStartedOutJsonAdapter(moshi)
                    DownloadStoppedOut::class.java -> DownloadStoppedOutJsonAdapter(moshi)
                    DownloadStatusOut::class.java -> DownloadStatusOutJsonAdapter(moshi)
                    BookDeletedOut::class.java -> BookDeletedOutJsonAdapter(moshi)
                    PriorityRequest::class.java -> PriorityRequestJsonAdapter(moshi)
                    PriorityOut::class.java -> PriorityOutJsonAdapter(moshi)
                    ErrorOut::class.java -> ErrorOutJsonAdapter(moshi)
                    else -> null
                }
                // Сгенерированные адаптеры не оборачивают себя в .nullSafe().
                // Обычно это делает встроенный поиск Moshi, но здесь адаптеры
                // регистрируются вручную (для R8), поэтому серверный JSON вида
                // "series": null ронял разбор всей карточки с JsonDataException.
                return adapter?.nullSafe()
            }
        }

        private val httpClient: OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
            .addInterceptor(
                HttpLoggingInterceptor().apply { level = HttpLoggingInterceptor.Level.BASIC },
            )
            .build()
    }
}
