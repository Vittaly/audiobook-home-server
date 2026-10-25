package ru.burkut.audiobooks.data.db

import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "playback_progress")
data class ProgressEntity(
    @PrimaryKey val bookId: Int,
    val chapterOrdinal: Int,
    val positionMs: Long,
    val updatedAt: Long,
)
