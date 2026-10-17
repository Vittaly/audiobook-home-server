package ru.burkut.audiobooks.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Scaffold
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.player.PlayerViewModel
import ru.burkut.audiobooks.ui.catalog.CatalogScreen
import ru.burkut.audiobooks.ui.components.MiniPlayer
import ru.burkut.audiobooks.ui.detail.BookDetailScreen
import ru.burkut.audiobooks.ui.player.PlayerScreen
import ru.burkut.audiobooks.ui.settings.ServerDialog
import ru.burkut.audiobooks.ui.setup.ServerSetupScreen
import ru.burkut.audiobooks.ui.theme.AppTheme
import ru.burkut.audiobooks.ui.viewModelFactoryOf

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = application as App
        setContent {
            AppTheme {
                val baseUrl by app.settings.baseUrl.collectAsState()
                val url = baseUrl
                if (url == null) {
                    ServerSetupScreen(app = app)
                } else {
                    AppContent(baseUrl = url)
                }
            }
        }
    }
}

@Composable
fun AppContent(baseUrl: String) {
    val navController = rememberNavController()

    // Один PlayerViewModel на весь граф навигации.
    // Живёт на уровне Activity, шарится между MiniPlayer, BookDetailScreen и PlayerScreen.
    val playerVm: PlayerViewModel = viewModel(
        factory = viewModelFactoryOf { PlayerViewModel(appInstance()) },
    )

    var showServerSettings by rememberSaveable { mutableStateOf(false) }

    Scaffold(
        bottomBar = {
            MiniPlayer(
                playerVm = playerVm,
                onClick = { navController.navigate("player") },
            )
        },
    ) { padding ->
        NavHost(
            navController = navController,
            startDestination = "catalog",
            modifier = Modifier.padding(padding),
        ) {
            composable("catalog") {
                CatalogScreen(
                    onOpenBook = { id -> navController.navigate("book/$id") },
                    onOpenSettings = { showServerSettings = true },
                )
            }

            composable("book/{bookId}") { entry ->
                val bookId = entry.arguments?.getString("bookId")?.toIntOrNull()
                if (bookId != null) {
                    BookDetailScreen(
                        bookId = bookId,
                        onBack = { navController.popBackStack() },
                        onPlay = { id ->
                            // Стартуем воспроизведение (torrent + DownloadManager + ExoPlayer)
                            // и сразу открываем полноэкранный плеер.
                            playerVm.playBook(id)
                            navController.navigate("player")
                        },
                        onPlayChapter = { book, chapter ->
                            playerVm.playChapter(book, chapter)
                            navController.navigate("player")
                        },
                    )
                }
            }

            composable("player") {
                // Передаём тот же playerVm явно — не даём PlayerScreen создавать
                // свой инстанс, чтобы MiniPlayer и полный экран смотрели одно состояние.
                PlayerScreen(
                    vm = playerVm,
                    onBack = { navController.popBackStack() },
                )
            }
        }
    }

    if (showServerSettings) {
        ServerDialog(
            currentUrl = baseUrl,
            onConnected = { showServerSettings = false },
            onDisconnected = { showServerSettings = false },
            onDismiss = { showServerSettings = false },
        )
    }
}