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
    // Исходный .part-путь (если открывали растущий файл). Пока он существует —
    // загрузка идёт; как только исчез (переименован в .mp3) — глава завершена.
    private var partPath: String? = null

    override fun addTransferListener(transferListener: TransferListener) = Unit

    override fun open(dataSpec: DataSpec): Long {
        var path = dataSpec.uri.path ?: throw IOException("Нет пути к файлу")
        if (path.endsWith(".part")) partPath = path

        // Гонка «.part → .mp3»: пока мы ждали появления растущего файла,
        // очередь могла доскачать главу и переименовать .part в .mp3 —
        // тогда открываем готовый файл.
        fun readyFallback(): String? =
            if (path.endsWith(".part")) {
                val ready = File(path.substringBeforeLast(".part"))
                if (ready.exists() && ready.length() > 0) ready.path else null
            } else {
                null
            }

        // Источник подтверждает готовность: ждём появления файла на диске.
        // Без этого ExoPlayer сразу падал бы с FileNotFoundException (ENOENT),
        // если очередь загрузки ещё не успела создать .part.
        val deadline = System.currentTimeMillis() + OPEN_TIMEOUT_MS
        while (true) {
            if (File(path).exists()) break
            val fallback = readyFallback()
            if (fallback != null) {
                path = fallback
                break
            }
            if (isCancelled()) {
                throw IOException("DataSource отменён до открытия файла: $path")
            }
            if (System.currentTimeMillis() >= deadline) {
                throw IOException(
                    "Файл не появился за $OPEN_TIMEOUT_MS мс — источник не готов: $path",
                )
            }
            Thread.sleep(WAIT_FOR_FILE_MS)
        }

        val raf = try {
            RandomAccessFile(path, "r")
        } catch (e: IOException) {
            // .part мог исчезнуть между ожиданием и открытием — пробуем .mp3.
            if (path.endsWith(".part")) {
                val ready = File(path.substringBeforeLast(".part"))
                if (ready.exists() && ready.length() > 0) {
                    RandomAccessFile(ready, "r")
                } else {
                    throw e
                }
            } else {
                throw e
            }
        }
        file = raf
        if (dataSpec.position > 0) raf.seek(dataSpec.position)
        // Сколько байт осталось прочитать от текущего положения до конца главы.
        // Если длина неизвестна (targetLength <= 0) — остаёмся без верхней границы
        // и читаем растущий файл, пока он не завершится (см. read()).
        val fromPos = if (dataSpec.position > 0) dataSpec.position else raf.filePointer
        remaining = if (targetLength > 0) (targetLength - fromPos).coerceAtLeast(0) else -1L
        return if (remaining == 0L) C.LENGTH_UNSET.toLong() else remaining
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (isCancelled()) return C.RESULT_END_OF_INPUT
        if (remaining == 0L) return C.RESULT_END_OF_INPUT

        val raf = file ?: throw IOException("DataSource закрыт")
        var bytesRead = 0
        while (bytesRead == 0) {
            if (isCancelled()) return C.RESULT_END_OF_INPUT
            val available = raf.length() - raf.filePointer
            if (available > 0) {
                // remaining < 0 — целевая длина неизвестна, читаем всё доступное.
                val toRead = if (remaining < 0) {
                    minOf(length.toLong(), available).toInt()
                } else {
                    minOf(length.toLong(), available, remaining).toInt()
                }
                bytesRead = raf.read(buffer, offset, toRead)
                if (bytesRead > 0 && remaining > 0) remaining -= bytesRead
            } else {
                // Ожидаем роста. Если длина неизвестна — завершением считаем
                // исчезновение .part (глава переименована в .mp3 и доскачана).
                if (remaining < 0 && !(partPath?.let { File(it).exists() } ?: false)) {
                    return C.RESULT_END_OF_INPUT
                }
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
        private const val WAIT_FOR_FILE_MS = 100L
        private const val OPEN_TIMEOUT_MS = 30_000L
    }
}
