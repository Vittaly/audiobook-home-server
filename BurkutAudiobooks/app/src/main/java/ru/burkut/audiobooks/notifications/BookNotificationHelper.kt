package ru.burkut.audiobooks.notifications

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat

/**
 * Отправка уведомлений «книга готова» и статуса загрузки.
 */
class BookNotificationHelper(private val context: Context) {

    private val notificationManager =
        context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    init {
        createChannel()
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_DOWNLOAD,
                "Загрузка книг",
                NotificationManager.IMPORTANCE_LOW,
            )
            channel.description = "Уведомления о готовности скачанных книг"
            notificationManager.createNotificationChannel(channel)

            val channelReady = NotificationChannel(
                CHANNEL_READY,
                "Книги готовы",
                NotificationManager.IMPORTANCE_DEFAULT,
            )
            notificationManager.createNotificationChannel(channelReady)
        }
    }

    private fun canNotify(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED

    fun notifyBookReady(bookTitle: String) {
        if (!canNotify()) return
        val notification = NotificationCompat.Builder(context, CHANNEL_READY)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentTitle("Книга готова")
            .setContentText(bookTitle)
            .setAutoCancel(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()
        notificationManager.notify(NOTIFY_READY_ID, notification)
    }

    companion object {
        private const val CHANNEL_DOWNLOAD = "download"
        private const val CHANNEL_READY = "book_ready"
        private const val NOTIFY_READY_ID = 1001
    }
}
