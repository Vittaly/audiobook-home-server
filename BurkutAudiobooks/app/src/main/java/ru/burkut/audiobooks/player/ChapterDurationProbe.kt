package ru.burkut.audiobooks.player

import android.media.MediaMetadataRetriever
import android.util.Log
import java.io.File

/**
 * Измерение реальной длительности аудиофайла на устройстве.
 *
 * Сервер не знает длительности глав (поле `duration_sec` из каталога равно null),
 * поэтому длительность определяет клиент по содержимому аудиофайла и сохраняет
 * локально (см. `chapters.localDurationSec`).
 *
 * Используется [MediaMetadataRetriever], который читает метаданные из самого
 * `.mp3` — это точная длительность в миллисекундах.
 */
object ChapterDurationProbe {

    private const val TAG = "ChapterDurationProbe"

    /**
     * Прочитать длительность файла и вернуть её в секундах (округление до целых секунд).
     *
     * @return null, если файла нет, он ещё растёт (`.part` скачан не до конца),
     *         или битый/нечитаемый — в этом случае длительность ещё неизвестна.
     */
    fun measureDurationSec(file: File): Int? {
        if (!file.exists() || file.length() <= 0L) return null
        val retriever = MediaMetadataRetriever()
        try {
            retriever.setDataSource(file.absolutePath)
            val ms = retriever.extractMetadata(
                MediaMetadataRetriever.METADATA_KEY_DURATION,
            )?.toLongOrNull() ?: return null
            if (ms <= 0L) return null
            val sec = ((ms + 500) / 1000L).toInt()
            Log.d(TAG, "duration " + file.name + " = " + sec)
            return sec
        } catch (e: Exception) {
            Log.d(TAG, "measure failed " + file.name + ": " + e.message)
            return null
        } finally {
            runCatching { retriever.release() }
        }
    }
}