package ru.burkut.audiobooks.player

import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile

/**
 * DataSource для воспроизведения локального файла книги.
 *
 * - Для готового `.mp3` читает файл напрямую (RandomAccessFile).
 * - Для растущего `.part` оборачивает в [GrowingFileDataSource], чтобы
 *   ExoPlayer читал по мере докачки главы.
 *
 * targetLength для растущего файла передаётся в DataSpec.length.
 */
@UnstableApi
class LocalBookDataSource(
    private val isCancelled: () -> Boolean = { false },
) : DataSource {

    private var raf: RandomAccessFile? = null
    private var growing: GrowingFileDataSource? = null
    private var remaining = 0L

    override fun addTransferListener(transferListener: TransferListener) = Unit

    override fun open(dataSpec: DataSpec): Long {
        val path = dataSpec.uri.path ?: throw IOException("Нет пути к файлу")
        val targetLength =
            dataSpec.length.takeIf { it != C.LENGTH_UNSET.toLong() && it > 0 } ?: -1L

        // Растущий .part — оборачиваем в GrowingFileDataSource всегда: он и ждёт
        // появления файла (если очередь ещё не создала его), и подхватывает
        // переименование .part → .mp3. Даже без известной длины он читает
        // растущий файл и возвращает C.LENGTH_UNSET.
        if (path.endsWith(".part")) {
            growing = GrowingFileDataSource(targetLength, isCancelled)
            return growing!!.open(dataSpec)
        }

        // Готовый файл — читаем напрямую.
        // Гонка: пока плеер открывал .part, очередь могла доскачать главу и
        // переименовать её в .mp3, а .part уже удалён. Тогда открываем .mp3.
        growing = null
        try {
            var openPath = path
            if (openPath.endsWith(".part") && !File(openPath).exists()) {
                val ready = File(openPath.substringBeforeLast(".part"))
                if (ready.exists() && ready.length() > 0) openPath = ready.path
            }
            val file = RandomAccessFile(openPath, "r")
            raf = file
            if (dataSpec.position > 0) file.seek(dataSpec.position)
            val available = file.length().coerceAtLeast(0)
            return available
        } catch (e: IOException) {
            // .part мог исчезнуть между проверкой и открытием — пробуем .mp3.
            if (path.endsWith(".part")) {
                val ready = File(path.substringBeforeLast(".part"))
                if (ready.exists() && ready.length() > 0) {
                    val file = RandomAccessFile(ready, "r")
                    raf = file
                    if (dataSpec.position > 0) file.seek(dataSpec.position)
                    return file.length().coerceAtLeast(0)
                }
            }
            throw e
        }
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        growing?.let { return it.read(buffer, offset, length) }

        val file = raf ?: throw IOException("DataSource закрыт")
        if (isCancelled()) return C.RESULT_END_OF_INPUT
        return file.read(buffer, offset, length)
    }

    override fun getUri() = null

    override fun close() {
        growing?.close()
        growing = null
        try {
            raf?.close()
        } catch (_: IOException) {
        }
        raf = null
    }
}
