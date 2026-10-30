package ru.burkut.audiobooks.player

import android.content.Context
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.ChapterEntity
import java.io.File

/**
 * Управление локальными файлами книг.
 *
 * Структура: filesDir/books/{bookHash}/{ordinal}.mp3 (+ {ordinal}.mp3.part во время загрузки)
 */
class BookStorage(context: Context) {

    private val root = File(context.filesDir, "books")

    fun bookDir(bookHash: String): File = File(root, bookHash)

    fun chapterFile(bookHash: String, ordinal: Int): File =
        File(bookDir(bookHash), "$ordinal.mp3")

    fun partFile(bookHash: String, ordinal: Int): File =
        File(bookDir(bookHash), "$ordinal.mp3.part")

    /** Локальный файл обложки книги (кэшируется, пока сервер доступен). */
    fun coverFile(bookHash: String): File = File(bookDir(bookHash), "cover.jpg")

    /** Локальный файл обложки раздачи (запасной вариант, если у книги нет своей). */
    fun releaseCoverFile(releaseId: Int): File =
        File(root, "release-$releaseId-cover.jpg")

    fun isChapterReady(chapter: ChapterEntity): Boolean {
        val f = chapterFile(chapter.bookId, chapter.ordinal)
        return f.exists() && f.length() > 0
    }

    fun isBookReady(book: BookEntity, chapters: List<ChapterEntity>): Boolean {
        return chapters.isNotEmpty() && chapters.all { isChapterReady(it) }
    }

    /**
     * Готова ли первая глава книги на устройстве.
     *
     * Сервер создаёт главы с ordinal, начиная с 1, поэтому «первая глава» —
     * это всегда файл "{FIRST_CHAPTER_ORDINAL}.mp3". Признак готовности —
     * существование непустого файла на диске (скачана при прослушивании).
     */
    fun isFirstChapterReady(bookHash: String): Boolean {
        val f = chapterFile(bookHash, FIRST_CHAPTER_ORDINAL)
        return f.exists() && f.length() > 0
    }

    /**
     * Состав воспроизводимого плейлиста — это задача источника, а не плеера.
     * Онлайн: все главы (недостающие докачает очередь, плеер стартует на растущем .part).
     * Оффлайн: только те, чей готовый `.mp3` реально лежит на диске — плеер не должен
     * ждать файлов, которых нет и не появятся.
     */
    fun playableChapters(chapters: List<ChapterEntity>, online: Boolean): List<ChapterEntity> =
        if (online) chapters else chapters.filter { isChapterReady(it) }

    /** Файл, который нужно отдавать плееру для главы: если растущий — .part, иначе готовый .mp3. */
    fun playbackFile(bookHash: String, ordinal: Int): File {
        val ready = chapterFile(bookHash, ordinal)
        return if (ready.exists() && ready.length() > 0) ready else partFile(bookHash, ordinal)
    }

    /** Очистка старого SimpleCache (вызывается при инициализации после обновления). */
    fun migrateAndClean(context: Context) {
        val legacy = File(context.filesDir, "audio-cache")
        if (legacy.exists()) {
            runCatching { legacy.deleteRecursively() }
        }
        if (!root.exists()) root.mkdirs()
    }

    companion object {
        /** Сервер нумерует главы с 1: первая глава книги — всегда файл "1.mp3". */
        const val FIRST_CHAPTER_ORDINAL = 1
    }
}
