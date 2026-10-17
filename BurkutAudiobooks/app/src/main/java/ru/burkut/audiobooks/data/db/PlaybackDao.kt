package ru.burkut.audiobooks.data.db

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

@Dao
interface PlaybackDao {

    @Query("SELECT * FROM playback_progress WHERE bookId = :bookId")
    suspend fun getProgress(bookId: Int): ProgressEntity?

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun upsert(entity: ProgressEntity)

    @Query("DELETE FROM playback_progress WHERE bookId = :bookId")
    suspend fun delete(bookId: Int)
}
