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
import ru.burkut.audiobooks.ui.release.ReleaseDetailScreen
import ru.burkut.audiobooks.ui.settings.ServerDialog
import ru.burkut.audiobooks.ui.theme.AppTheme
import ru.burkut.audiobooks.ui.viewModelFactoryOf

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = application as App
        setContent {
            AppTheme {
                AppContent()
            }
        }
    }
}

@Composable
fun AppContent() {
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
                    onOpenRelease = { id -> navController.navigate("release/$id") },
                    onOpenSettings = { showServerSettings = true },
                )
            }

            composable("release/{releaseId}") { entry ->
                val releaseId = entry.arguments?.getString("releaseId")?.toIntOrNull()
                if (releaseId != null) {
                    ReleaseDetailScreen(
                        releaseId = releaseId,
                        onBack = { navController.popBackStack() },
                        onOpenBook = { id -> navController.navigate("book/$id") },
                        onPlayRelease = {
                            // Играем ВСЮ раздачу с самого начала, не зная заранее
                            // сколько в ней книг. Загрузку стартуем на уровне раздачи:
                            // сервер не создаёт книгу-заглушку, книги появляются
                            // только по факту сканирования структуры торрента.
                            android.util.Log.d("MainActivity", "onPlayRelease releaseId=$releaseId")
                            playerVm.playRelease(releaseId)
                            navController.navigate("player")
                        },
                    )
                }
            }

            composable("book/{bookId}") { entry ->
                val bookId = entry.arguments?.getString("bookId")?.toIntOrNull()
                if (bookId != null) {
                    BookDetailScreen(
                        bookId = bookId,
                        onBack = { navController.popBackStack() },
                        onPlay = { id ->
                            // Стартуем воспроизведение (локальный файл + докачка через очередь)
                            // и сразу открываем полноэкранный плеер.
                            playerVm.playBook(id)
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
            currentUrl = appInstance().settings.baseUrl.value,
            onConnected = { showServerSettings = false },
            onDisconnected = { showServerSettings = false },
            onDismiss = { showServerSettings = false },
        )
    }
}