package ru.burkut.audiobooks.player

import android.app.PendingIntent
import android.content.Intent
import android.os.Handler
import android.os.Looper
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.session.MediaSession
import androidx.media3.session.MediaSessionService
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.ui.MainActivity
import ru.burkut.audiobooks.data.db.ProgressEntity

@UnstableApi
class PlayerService : MediaSessionService() {

    private var mediaSession: MediaSession? = null

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val handler = Handler(Looper.getMainLooper())

    private val playbackDao by lazy { (application as App).db.playbackDao() }

    private val saveTick = object : Runnable {
        override fun run() {
            saveCurrent()
            handler.postDelayed(this, SAVE_INTERVAL_MS)
        }
    }

    private val playerListener = object : Player.Listener {
        override fun onPlayWhenReadyChanged(playWhenReady: Boolean, reason: Int) {
            if (!playWhenReady) saveCurrent()
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            if (!isPlaying) saveCurrent()
        }

        override fun onPositionDiscontinuity(
            oldPosition: Player.PositionInfo,
            newPosition: Player.PositionInfo,
            reason: Int,
        ) {
            if (reason == Player.DISCONTINUITY_REASON_AUTO_TRANSITION) {
                saveProgress(oldPosition.mediaItem?.mediaId, oldPosition.positionMs)
            }
        }
    }

    override fun onCreate() {
        super.onCreate()
        val app = application as App
        val player = ExoPlayer.Builder(this)
            .setMediaSourceFactory(app.localMediaSourceFactory())
            .build()
            .apply {
                setHandleAudioBecomingNoisy(true)
                addListener(playerListener)
            }
        val sessionActivity = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        mediaSession = MediaSession.Builder(this, player)
            .setSessionActivity(sessionActivity)
            .build()
        handler.postDelayed(saveTick, SAVE_INTERVAL_MS)
    }

    override fun onGetSession(controllerInfo: MediaSession.ControllerInfo): MediaSession? =
        mediaSession

    private fun saveCurrent() {
        mediaSession?.player?.let { player ->
            saveProgress(player.currentMediaItem?.mediaId, player.currentPosition)
        }
    }

    private fun saveProgress(mediaId: String?, positionMs: Long) {
        val ids = parseMediaId(mediaId) ?: return
        scope.launch {
            playbackDao.upsert(
                ProgressEntity(
                    bookId = ids.first,
                    chapterOrdinal = ids.second,
                    positionMs = positionMs,
                    updatedAt = System.currentTimeMillis(),
                ),
            )
        }
    }

    override fun onDestroy() {
        handler.removeCallbacks(saveTick)
        mediaSession?.release()
        mediaSession = null
        super.onDestroy()
    }

    companion object {
        private const val SAVE_INTERVAL_MS = 10_000L

        /** mediaId формата "bookHash:chapterOrdinal". */
        fun parseMediaId(mediaId: String?): Pair<String, Int>? {
            if (mediaId == null) return null
            val parts = mediaId.split(":")
            if (parts.size != 2) return null
            val bookHash = parts[0]
            val chapterOrdinal = parts[1].toIntOrNull() ?: return null
            return bookHash to chapterOrdinal
        }
    }
}
