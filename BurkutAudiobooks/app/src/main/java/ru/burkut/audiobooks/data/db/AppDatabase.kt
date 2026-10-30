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
    version = 6,
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

        /** Версия 3: локальная длительность глав (измеряется клиентом по аудиофайлу). */
        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL(
                    "ALTER TABLE chapters ADD COLUMN localDurationSec INTEGER"
                )
            }
        }

        /** Версия 4: серия книги в кэше (для фильтрации каталога по сериям). */
        private val MIGRATION_3_4 = object : Migration(3, 4) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL("ALTER TABLE books ADD COLUMN seriesId INTEGER")
                database.execSQL("ALTER TABLE books ADD COLUMN seriesTitle TEXT")
                database.execSQL("ALTER TABLE books ADD COLUMN seriesOrder INTEGER")
            }
        }

        /**
         * Версия 5: дата релиза с трекера (releasedAt).
         *
         * Для уже закэшированных раздач заполняем releasedAt значением addedAt:
         * порядок карточек не «прыгнет» до следующей синхронизации, а после
         * неё придёт реальная дата трекера.
         */
        private val MIGRATION_4_5 = object : Migration(4, 5) {
            override fun migrate(database: SupportSQLiteDatabase) {
                database.execSQL("ALTER TABLE releases ADD COLUMN releasedAt TEXT")
                database.execSQL(
                    "UPDATE releases SET releasedAt = addedAt WHERE releasedAt IS NULL"
                )
            }
        }

        /**
         * Версия 6: bookId Int → String (SHA256 hash), book_hash rename.
         *
         * SQLite не поддерживает ALTER COLUMN для изменения типа, поэтому
         * пересоздаём таблицы books, chapters, playback_progress с новыми типами.
         */
        private val MIGRATION_5_6 = object : Migration(5, 6) {
            override fun migrate(database: SupportSQLiteDatabase) {
                // 1. backup existing data
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS books_old AS SELECT * FROM books"
                )
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS chapters_old AS SELECT * FROM chapters"
                )
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS playback_progress_old AS SELECT * FROM playback_progress"
                )

                // 2. drop old tables
                database.execSQL("DROP TABLE IF EXISTS books")
                database.execSQL("DROP TABLE IF EXISTS chapters")
                database.execSQL("DROP TABLE IF EXISTS playback_progress")

                // 3. recreate books with TEXT primary key
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS books (" +
                        "id TEXT NOT NULL PRIMARY KEY, " +
                        "releaseId INTEGER, ordinal INTEGER NOT NULL, title TEXT NOT NULL, " +
                        "folder TEXT, author TEXT, narrator TEXT, year INTEGER, language TEXT, " +
                        "description TEXT, rubrics TEXT, status TEXT NOT NULL, " +
                        "chaptersCount INTEGER NOT NULL DEFAULT 0, chaptersReady INTEGER NOT NULL DEFAULT 0, " +
                        "totalDurationSec INTEGER, sizeBytes INTEGER, coverUrl TEXT, " +
                        "coverIsTemp INTEGER NOT NULL DEFAULT 0, addedAt TEXT NOT NULL, lastAccessed TEXT, " +
                        "seriesId INTEGER, seriesTitle TEXT, seriesOrder INTEGER, " +
                        "isProvisional INTEGER NOT NULL DEFAULT 0, " +
                        "localStatus TEXT NOT NULL DEFAULT 'absent', " +
                        "downloadedBytes INTEGER NOT NULL DEFAULT 0, localTotalBytes INTEGER NOT NULL DEFAULT 0)"
                )

                // 4. recreate chapters with TEXT bookId
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS chapters (" +
                        "id INTEGER NOT NULL PRIMARY KEY, " +
                        "bookId TEXT NOT NULL, ordinal INTEGER NOT NULL, title TEXT, " +
                        "durationSec INTEGER, sizeBytes INTEGER NOT NULL, status TEXT NOT NULL, " +
                        "progress REAL NOT NULL DEFAULT 0, " +
                        "localStatus TEXT NOT NULL DEFAULT 'absent', downloadedBytes INTEGER NOT NULL DEFAULT 0, " +
                        "localDurationSec INTEGER)"
                )

                // 5. recreate playback_progress with TEXT bookId
                database.execSQL(
                    "CREATE TABLE IF NOT EXISTS playback_progress (" +
                        "bookId TEXT NOT NULL PRIMARY KEY, " +
                        "chapterOrdinal INTEGER NOT NULL, " +
                        "positionMs INTEGER NOT NULL, " +
                        "updatedAt INTEGER NOT NULL)"
                )

                // 6. migrate data — cast Int id to TEXT (string representation of int)
                database.execSQL(
                    "INSERT INTO books (id, releaseId, ordinal, title, folder, author, narrator, " +
                        "year, language, description, status, chaptersCount, chaptersReady, " +
                        "totalDurationSec, sizeBytes, coverUrl, coverIsTemp, addedAt, lastAccessed, " +
                        "seriesId, seriesTitle, seriesOrder, localStatus, downloadedBytes, localTotalBytes) " +
                        "SELECT CAST(id AS TEXT), releaseId, ordinal, title, folder, author, narrator, " +
                        "year, language, description, status, chaptersCount, chaptersReady, " +
                        "totalDurationSec, sizeBytes, coverUrl, coverIsTemp, addedAt, lastAccessed, " +
                        "seriesId, seriesTitle, seriesOrder, localStatus, downloadedBytes, localTotalBytes " +
                        "FROM books_old"
                )
                database.execSQL(
                    "INSERT INTO chapters (id, bookId, ordinal, title, durationSec, sizeBytes, status, " +
                        "progress, localStatus, downloadedBytes, localDurationSec) " +
                        "SELECT id, CAST(bookId AS TEXT), ordinal, title, durationSec, sizeBytes, status, " +
                        "progress, localStatus, downloadedBytes, localDurationSec FROM chapters_old"
                )
                database.execSQL(
                    "INSERT INTO playback_progress (bookId, chapterOrdinal, positionMs, updatedAt) " +
                        "SELECT CAST(bookId AS TEXT), chapterOrdinal, positionMs, updatedAt " +
                        "FROM playback_progress_old"
                )

                // 7. drop backup tables
                database.execSQL("DROP TABLE IF EXISTS books_old")
                database.execSQL("DROP TABLE IF EXISTS chapters_old")
                database.execSQL("DROP TABLE IF EXISTS playback_progress_old")
            }
        }

        fun get(context: Context): AppDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "audiobooks.db",
                )
                    .addMigrations(
                        MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4, MIGRATION_4_5, MIGRATION_5_6,
                    )
                    .build()
                    .also { instance = it }
            }
    }
}
