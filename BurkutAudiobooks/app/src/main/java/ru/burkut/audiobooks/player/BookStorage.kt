package ru.burkut.audiobooks.player

import android.content.Context
import ru.burkut.audiobooks.data.db.BookEntity
import ru.burkut.audiobooks.data.db.ChapterEntity
import java.io.File

/**
 * Управление локальными файлами книг.
 *
 * Структура: filesDir/books/{bookId}/{ordinal}.mp3 (+ {ordinal}.mp3.part во время загрузки)
 */
class BookStorage(context: Context) {

    private val root = File(context.filesDir, "books")

    fun bookDir(bookId: Int): File = File(root, bookId.toString())

    fun chapterFile(bookId: Int, ordinal: Int): File =
        File(bookDir(bookId), "$ordinal.mp3")

    fun partFile(bookId: Int, ordinal: Int): File =
        File(bookDir(bookId), "$ordinal.mp3.part")

    /** Локальный файл обложки книги (кэшируется, пока сервер доступен). */
    fun coverFile(bookId: Int): File = File(bookDir(bookId), "cover.jpg")

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
     * Состав воспроизводимого плейлиста — это задача источника, а не плеера.
     * Онлайн: все главы (недостающие докачает очередь, плеер стартует на растущем .part).
     * Оффлайн: только те, чей готовый `.mp3` реально лежит на диске — плеер не должен
     * ждать файлов, которых нет и не появятся.
     */
    fun playableChapters(chapters: List<ChapterEntity>, online: Boolean): List<ChapterEntity> =
        if (online) chapters else chapters.filter { isChapterReady(it) }

    /** Файл, который нужно отдавать плееру для главы: если растущий — .part, иначе готовый .mp3. */
    fun playbackFile(bookId: Int, ordinal: Int): File {
        val ready = chapterFile(bookId, ordinal)
        return if (ready.exists() && ready.length() > 0) ready else partFile(bookId, ordinal)
    }

    /** Очистка старого SimpleCache (вызывается при инициализации после обновления). */
    fun migrateAndClean(context: Context) {
        val legacy = File(context.filesDir, "audio-cache")
        if (legacy.exists()) {
            runCatching { legacy.deleteRecursively() }
        }
        if (!root.exists()) root.mkdirs()
    }
}
