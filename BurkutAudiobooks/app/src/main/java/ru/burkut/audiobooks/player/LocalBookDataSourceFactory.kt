package ru.burkut.audiobooks.player

import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import java.io.File
import java.util.concurrent.ConcurrentHashMap

/**
 * Фабрика локальных источников для ExoPlayer.
 *
 * ExoPlayer вызывает [DataSource.Factory.createDataSource] без параметров —
 * URI появляется только в [DataSource.open]. Поэтому файл резолвится внутри
 * [DataSource.open]: по пути из URI (`/books/{bookId}/{ordinal}.mp3[.part]`)
 * берём актуальный файл через [resolveFile] (который учитывает, что .part
 * мог уже превратиться в .mp3) и подставляем его в DataSpec.
 *
 * Для растущего `.part` нужна целевая длина главы — она регистрируется
 * заранее через [setTargetLength] (из PlayerViewModel при построении плейлиста).
 */
@UnstableApi
class LocalBookDataSourceFactory(
    private val resolveFile: (bookId: Int, ordinal: Int) -> File,
) : DataSource.Factory {

    private val targetLengths = ConcurrentHashMap<String, Long>()

    /** Регистрирует целевую длину главы (для растущего .part). */
    fun setTargetLength(bookId: Int, ordinal: Int, sizeBytes: Long) {
        if (sizeBytes > 0) targetLengths[key(bookId, ordinal)] = sizeBytes
    }

    override fun createDataSource(): DataSource = object : DataSource {
        private val local = LocalBookDataSource()

        override fun addTransferListener(transferListener: TransferListener) {
            local.addTransferListener(transferListener)
        }

        override fun open(dataSpec: DataSpec): Long {
            val parsed = parseUri(dataSpec.uri.path) ?: return local.open(dataSpec)
            val (bookId, ordinal) = parsed
            val file = resolveFile(bookId, ordinal)
            val isPart = file.path.endsWith(".part")
            val target = targetLengths[key(bookId, ordinal)] ?: -1L
            val length = if (isPart && target > 0) target else C.LENGTH_UNSET.toLong()
            val adjusted = dataSpec.buildUpon()
                .setUri(android.net.Uri.fromFile(file))
                .setLength(length)
                .build()
            return local.open(adjusted)
        }

        override fun read(buffer: ByteArray, offset: Int, length: Int): Int =
            local.read(buffer, offset, length)

        override fun getUri() = local.getUri()

        override fun close() = local.close()
    }

    /** Возвращает (bookId, ordinal) из пути `/books/{id}/{ordinal}.mp3[.part]`. */
    private fun parseUri(path: String?): Pair<Int, Int>? {
        if (path == null) return null
        val m = Regex("/books/(\\d+)/(\\d+)\\.mp3(?:\\..*)?$").matchEntire(path) ?: return null
        val bookId = m.groups[1]?.value?.toIntOrNull() ?: return null
        val ordinal = m.groups[2]?.value?.toIntOrNull() ?: return null
        return bookId to ordinal
    }

    private companion object {
        fun key(bookId: Int, ordinal: Int): String = "$bookId:$ordinal"
    }
}
