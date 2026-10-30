package ru.burkut.audiobooks.player

import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import java.io.File
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap

/**
 * Фабрика локальных источников для ExoPlayer.
 *
 * ExoPlayer вызывает [DataSource.Factory.createDataSource] без параметров —
 * URI появляется только в [DataSource.open]. Поэтому файл резолвится внутри
 * [DataSource.open]: по пути из URI (`/books/{bookHash}/{ordinal}.mp3[.part]`)
 * берём актуальный файл через [resolveFile] (который учитывает, что .part
 * мог уже превратиться в .mp3) и подставляем его в DataSpec.
 *
 * Для растущего `.part` нужна целевая длина главы — она регистрируется
 * заранее через [setTargetLength] (из PlayerViewModel при построении плейлиста).
 */
@UnstableApi
class LocalBookDataSourceFactory(
    private val resolveFile: (bookHash: String, ordinal: Int) -> File,
    /** Доступен ли сервер: оффлайн источник не ждёт появления файла, а падает сразу. */
    private val isOnline: () -> Boolean = { true },
) : DataSource.Factory {

    private val targetLengths = ConcurrentHashMap<String, Long>()

    /** Регистрирует целевую длину главы (для растущего .part). */
    fun setTargetLength(bookHash: String, ordinal: Int, sizeBytes: Long) {
        if (sizeBytes > 0) targetLengths[key(bookHash, ordinal)] = sizeBytes
    }

    override fun createDataSource(): DataSource = object : DataSource {
        private val local = LocalBookDataSource()

        override fun addTransferListener(transferListener: TransferListener) {
            local.addTransferListener(transferListener)
        }

        override fun open(dataSpec: DataSpec): Long {
            val parsed = parseUri(dataSpec.uri.path) ?: return local.open(dataSpec)
            val (bookHash, ordinal) = parsed
            val file = resolveFile(bookHash, ordinal)
            // Источник сам решает, доступна ли глава: если файла нет и сервер
            // оффлайн — ждать нечего, падаем сразу, а не висим 30 секунд в
            // GrowingFileDataSource на несуществующем .part.
            if (!isOnline() && !(file.exists() && file.length() > 0)) {
                throw IOException("Глава недоступна локально, сервер оффлайн: ${file.path}")
            }
            val isPart = file.path.endsWith(".part")
            val target = targetLengths[key(bookHash, ordinal)] ?: -1L
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

    /** Возвращает (bookHash, ordinal) из пути `/books/{hash}/{ordinal}.mp3[.part]`. */
    private fun parseUri(path: String?): Pair<String, Int>? {
        if (path == null) return null
        val m = Regex("/books/([a-f0-9]+)/(\\d+)\\.mp3(?:\\..*)?$").matchEntire(path) ?: return null
        val bookHash = m.groups[1]?.value ?: return null
        val ordinal = m.groups[2]?.value?.toIntOrNull() ?: return null
        return bookHash to ordinal
    }

    private companion object {
        fun key(bookHash: String, ordinal: Int): String = "$bookHash:$ordinal"
    }
}
