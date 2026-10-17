package ru.burkut.audiobooks.player

import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.TransferListener
import java.io.IOException
import java.io.RandomAccessFile

/**
 * DataSource для воспроизведения файла, который ещё пишется (растущий `.part`).
 *
 * Считывает данные по мере их появления и ждёт, пока станут доступны новые байты.
 * Размер главы известен заранее ([targetLength]); как только файл вырос до этого
 * размера и данные прочитаны — воспроизведение главы завершается нормально.
 *
 * Позволяет начинать воспроизведение сразу, не дожидаясь полной загрузки главы
 * (гибрид с явными файлами).
 */
@UnstableApi
class GrowingFileDataSource(
    private val targetLength: Long,
    private val isCancelled: () -> Boolean = { false },
) : DataSource {

    private var file: RandomAccessFile? = null
    private var remaining = 0L

    override fun addTransferListener(transferListener: TransferListener) = Unit

    override fun open(dataSpec: DataSpec): Long {
        val path = dataSpec.uri.path ?: throw IOException("Нет пути к файлу")
        val raf = RandomAccessFile(path, "r")
        file = raf
        if (dataSpec.position > 0) raf.seek(dataSpec.position)
        // Сколько байт осталось прочитать от текущего положения до конца главы.
        val fromPos = if (dataSpec.position > 0) dataSpec.position else raf.filePointer
        remaining = (targetLength - fromPos).coerceAtLeast(0)
        return if (remaining == 0L) C.LENGTH_UNSET.toLong() else remaining
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (isCancelled()) return C.RESULT_END_OF_INPUT
        if (remaining <= 0) return C.RESULT_END_OF_INPUT

        val raf = file ?: throw IOException("DataSource закрыт")
        var bytesRead = 0
        while (bytesRead == 0) {
            if (isCancelled()) return C.RESULT_END_OF_INPUT
            val available = raf.length() - raf.filePointer
            if (available > 0) {
                val toRead = minOf(length.toLong(), available, remaining).toInt()
                bytesRead = raf.read(buffer, offset, toRead)
                if (bytesRead > 0) remaining -= bytesRead
            } else {
                // Файл ещё не вырос — ждём новые байты.
                Thread.sleep(WAIT_FOR_DATA_MS)
            }
        }
        return bytesRead
    }

    override fun getUri() = null

    override fun close() {
        try {
            file?.close()
        } catch (_: IOException) {
        }
        file = null
    }

    companion object {
        private const val WAIT_FOR_DATA_MS = 80L
    }
}
