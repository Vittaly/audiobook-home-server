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

    fun isChapterReady(chapter: ChapterEntity): Boolean {
        val f = chapterFile(chapter.bookId, chapter.ordinal)
        return f.exists() && f.length() > 0
    }

    fun isBookReady(book: BookEntity, chapters: List<ChapterEntity>): Boolean {
        return chapters.isNotEmpty() && chapters.all { isChapterReady(it) }
    }

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
