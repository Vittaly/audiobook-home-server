package ru.burkut.audiobooks.player

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.ResponseBody
import ru.burkut.audiobooks.data.BookRepository
import ru.burkut.audiobooks.data.api.ApiProvider
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * Локальный кэш обложек книг и раздач.
 *
 * На сервере обложка всегда доступна по URL (`/books/{id}/cover` и
 * `/releases/{id}/cover`), но плеер должен показывать картинку и в оффлайне.
 * Поэтому, пока сервер доступен, обложка сохраняется в файл рядом с аудио
 * (`filesDir/books/{bookId}/cover.jpg`). Плеер и UI затем отдают Coil локальный
 * `file://` URI — без обращения к серверу.
 *
 * Хранение — это задача слоя данных (а не плеера или источника): плеер лишь
 * спрашивает «есть ли локальная обложка» и подставляет её URI.
 */
class CoverStore(
    private val apiProvider: ApiProvider,
    private val repository: BookRepository,
    private val storage: BookStorage,
) {

    /** Ключи (book-N / release-N), обложки которых качаются прямо сейчас. */
    private val inflight = ConcurrentHashMap<String, Boolean>()

    /** Локальный файл обложки книги, если он уже сохранён. */
    fun cachedBookCover(bookId: Int): File? =
        storage.coverFile(bookId).takeIf { it.exists() && it.length() > 0 }

    /** Локальный файл обложки раздачи, если он уже сохранён. */
    fun cachedReleaseCover(releaseId: Int): File? =
        storage.releaseCoverFile(releaseId).takeIf { it.exists() && it.length() > 0 }

    /**
     * Гарантирует наличие локальной обложки книги.
     *
     * @param force перезаписать даже если файл уже есть — нужно для смены
     *        временной обложки (карточки раздачи) на финальную.
     * @return локальный файл, если он есть/получился, иначе null.
     */
    suspend fun ensureBookCover(bookId: Int, force: Boolean = false): File? {
        val target = storage.coverFile(bookId)
        if (!force && target.exists() && target.length() > 0) return target
        return fetch("book-$bookId", target) { apiProvider.get().getCover(bookId) }
    }

    /** Гарантирует наличие локальной обложки раздачи. */
    suspend fun ensureReleaseCover(releaseId: Int, force: Boolean = false): File? {
        val target = storage.releaseCoverFile(releaseId)
        if (!force && target.exists() && target.length() > 0) return target
        return fetch("release-$releaseId", target) { apiProvider.get().getReleaseCover(releaseId) }
    }

    /**
     * Догружает обложки уже скачанных книг, у которых локального файла ещё нет.
     *
     * Нужно, чтобы книги, загруженные до появления кэша обложек, тоже стали
     * доступны оффлайн. Вызывается при появлении сервера; пока сервера нет —
     * все сетевые ошибки проглатываются, ничего не ломается.
     */
    suspend fun warmUpReadyBooks() {
        val books = runCatching { repository.getAllBooks() }.getOrDefault(emptyList())
        for (book in books) {
            if (cachedBookCover(book.id) != null) continue
            val chapters = runCatching { repository.getChapters(book.id) }
                .getOrDefault(emptyList())
            if (chapters.isEmpty()) continue
            // Только полностью скачанные (играбельные оффлайн) книги.
            if (chapters.all { storage.isChapterReady(it) }) {
                ensureBookCover(book.id)
            }
        }
    }

    /**
     * Скачивает картинку во временный файл и атомарно подменяет целевой.
     * Идемпотентно: повторный вызов для того же ключа, пока идёт загрузка,
     * не создаёт второй запрос.
     */
    private suspend fun fetch(
        key: String,
        target: File,
        request: suspend () -> ResponseBody,
    ): File? {
        if (inflight.putIfAbsent(key, true) != null) {
            return target.takeIf { it.exists() && it.length() > 0 }
        }
        return try {
            withContext(Dispatchers.IO) {
                target.parentFile?.mkdirs()
                val tmp = File(target.parentFile, "${target.name}.tmp")
                request().byteStream().use { input ->
                    tmp.outputStream().use { output -> input.copyTo(output) }
                }
                if (target.exists()) target.delete()
                tmp.renameTo(target)
                Log.d(TAG, "cover cached key=$key -> ${target.absolutePath}")
                target
            }
        } catch (e: Exception) {
            Log.d(TAG, "cover fetch failed key=$key err=${e.message}")
            target.takeIf { it.exists() && it.length() > 0 }
        } finally {
            inflight.remove(key)
        }
    }

    companion object {
        private const val TAG = "CoverStore"
    }
}
