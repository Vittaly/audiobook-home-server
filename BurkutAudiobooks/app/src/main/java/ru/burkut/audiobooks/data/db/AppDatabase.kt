package ru.burkut.audiobooks.data.db

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [
        ProgressEntity::class,
        ReleaseEntity::class,
        BookEntity::class,
        ChapterEntity::class,
    ],
    version = 2,
    exportSchema = false,
)
abstract class AppDatabase : RoomDatabase() {

    abstract fun playbackDao(): PlaybackDao

    abstract fun catalogDao(): CatalogDao

    companion object {
        @Volatile
        private var instance: AppDatabase? = null

        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(database: SupportSQLiteDatabase) {
                // Прогресс: chapterId -> chapterOrdinal.
                database.execSQL(
                    "ALTER TABLE playback_progress RENAME TO playback_progress_old"
                )
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS playback_progress (" +
                        "bookId INTEGER NOT NULL PRIMARY KEY, " +
                        "chapterOrdinal INTEGER NOT NULL, " +
                        "positionMs INTEGER NOT NULL, " +
                        "updatedAt INTEGER NOT NULL)"
                )
                database.execSQL(
                    "INSERT INTO playback_progress (bookId, chapterOrdinal, positionMs, updatedAt) " +
                        "SELECT bookId, chapterId, positionMs, updatedAt FROM playback_progress_old"
                )
                database.execSQL("DROP TABLE playback_progress_old")

                // Новые таблицы кэша каталога.
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS releases (" +
                        "id INTEGER NOT NULL PRIMARY KEY, " +
                        "title TEXT NOT NULL, titleRaw TEXT, author TEXT, narrator TEXT, " +
                        "year INTEGER, language TEXT, description TEXT, rubrics TEXT, " +
                        "status TEXT NOT NULL, contentType TEXT NOT NULL DEFAULT 'single', " +
                        "booksCount INTEGER NOT NULL DEFAULT 0, booksReady INTEGER NOT NULL DEFAULT 0, " +
                        "chaptersCount INTEGER NOT NULL DEFAULT 0, chaptersReady INTEGER NOT NULL DEFAULT 0, " +
                        "totalDurationSec INTEGER, sizeBytes INTEGER, coverUrl TEXT, " +
                        "addedAt TEXT NOT NULL, downloadedAt TEXT, lastAccessed TEXT, " +
                        "queuedForDevice INTEGER NOT NULL DEFAULT 0)"
                )
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS books (" +
                        "id INTEGER NOT NULL PRIMARY KEY, " +
                        "releaseId INTEGER NOT NULL, ordinal INTEGER NOT NULL, title TEXT NOT NULL, " +
                        "folder TEXT, author TEXT, narrator TEXT, year INTEGER, language TEXT, " +
                        "description TEXT, status TEXT NOT NULL, " +
                        "chaptersCount INTEGER NOT NULL DEFAULT 0, chaptersReady INTEGER NOT NULL DEFAULT 0, " +
                        "totalDurationSec INTEGER, sizeBytes INTEGER, coverUrl TEXT, " +
                        "coverIsTemp INTEGER NOT NULL DEFAULT 0, addedAt TEXT NOT NULL, lastAccessed TEXT, " +
                        "localStatus TEXT NOT NULL DEFAULT 'absent', " +
                        "downloadedBytes INTEGER NOT NULL DEFAULT 0, localTotalBytes INTEGER NOT NULL DEFAULT 0)"
                )
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS chapters (" +
                        "id INTEGER NOT NULL PRIMARY KEY, " +
                        "bookId INTEGER NOT NULL, ordinal INTEGER NOT NULL, title TEXT, " +
                        "durationSec INTEGER, sizeBytes INTEGER NOT NULL, status TEXT NOT NULL, " +
                        "progress REAL NOT NULL DEFAULT 0, " +
                        "localStatus TEXT NOT NULL DEFAULT 'absent', downloadedBytes INTEGER NOT NULL DEFAULT 0)"
                )
            }
        }

        fun get(context: Context): AppDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "audiobooks.db",
                )
                    .addMigrations(MIGRATION_1_2)
                    .build()
                    .also { instance = it }
            }
    }
}
