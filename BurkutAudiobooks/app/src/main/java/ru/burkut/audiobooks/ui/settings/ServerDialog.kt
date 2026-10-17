package ru.burkut.audiobooks.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.mdns.DiscoveryStatus
import ru.burkut.audiobooks.ui.appInstance
import ru.burkut.audiobooks.ui.setup.ServerDiscoveryViewModel
import ru.burkut.audiobooks.ui.setup.connectToServer
import ru.burkut.audiobooks.ui.viewModelFactoryOf

@Composable
fun ServerDialog(
    currentUrl: String,
    onConnected: () -> Unit,
    onDisconnected: () -> Unit,
    onDismiss: () -> Unit,
) {
    val context = LocalContext.current
    val app = context.applicationContext as App
    val vm: ServerDiscoveryViewModel = viewModel(
        factory = viewModelFactoryOf { ServerDiscoveryViewModel(appInstance()) }
    )
    val servers by vm.servers.collectAsState()
    val status by vm.status.collectAsState()
    val scope = rememberCoroutineScope()

    var mode by remember { mutableStateOf("info") }
    var manualHost by remember { mutableStateOf("") }
    var manualPort by remember { mutableStateOf("8000") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    DisposableEffect(mode) {
        if (mode == "info") vm.stop() else vm.start()
        onDispose { vm.stop() }
    }

    Dialog(onDismissRequest = onDismiss) {
        Card(modifier = Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier
                    .fillMaxWidth()
                    .verticalScroll(rememberScrollState())
                    .padding(20.dp),
                verticalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Text("Сервер", style = MaterialTheme.typography.titleLarge)

                if (mode == "info") {
                    Text(currentUrl, style = MaterialTheme.typography.bodyLarge)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = { mode = "search" }) {
                            Text("Найти заново")
                        }
                        TextButton(onClick = { mode = "manual" }) {
                            Text("Ввести вручную")
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        TextButton(onClick = {
                            app.settings.clearServer()
                            onDisconnected()
                        }) {
                            Text("Отключиться", color = MaterialTheme.colorScheme.error)
                        }
                        Spacer(Modifier.weight(1f))
                        TextButton(onClick = onDismiss) {
                            Text("Отмена")
                        }
                    }
                }

                if (mode == "search" || mode == "manual") {
                    when (status) {
                        is DiscoveryStatus.Searching -> Row(
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            CircularProgressIndicator(Modifier.padding(end = 8.dp))
                            Text("Поиск серверов…")
                        }
                        is DiscoveryStatus.Error -> Text(
                            (status as DiscoveryStatus.Error).message,
                            color = MaterialTheme.colorScheme.error,
                        )
                        else -> Unit
                    }

                    if (mode == "search") {
                        if (servers.isEmpty() && status !is DiscoveryStatus.Searching) {
                            Text("Серверы не найдены в сети.")
                        }
                        servers.forEach { server ->
                            Card(modifier = Modifier.fillMaxWidth()) {
                                Column(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .padding(12.dp),
                                ) {
                                    Text(server.name, style = MaterialTheme.typography.titleMedium)
                                    Text("${server.host}:${server.port}")
                                    Spacer(Modifier.height(8.dp))
                                    Button(
                                        onClick = {
                                            busy = true
                                            connectToServer(
                                                app = app,
                                                scope = scope,
                                                url = server.url,
                                                onSuccess = { busy = false; onConnected() },
                                                onError = { busy = false; error = it },
                                            )
                                        },
                                        enabled = !busy,
                                    ) {
                                        Text("Подключиться")
                                    }
                                }
                            }
                        }
                    }

                    if (mode == "manual") {
                        OutlinedTextField(
                            value = manualHost,
                            onValueChange = { manualHost = it },
                            label = { Text("IP-адрес или хост") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                        OutlinedTextField(
                            value = manualPort,
                            onValueChange = { manualPort = it.filter(Char::isDigit) },
                            label = { Text("Порт") },
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                            modifier = Modifier.fillMaxWidth(),
                        )
                        Button(
                            onClick = {
                                val host = manualHost.trim()
                                val port = manualPort.toIntOrNull()
                                if (host.isBlank() || port == null || port !in 1..65535) {
                                    error = "Введите корректный хост и порт"
                                    return@Button
                                }
                                busy = true
                                connectToServer(
                                    app = app,
                                    scope = scope,
                                    url = "http://$host:$port/",
                                    onSuccess = { busy = false; onConnected() },
                                    onError = { busy = false; error = it },
                                )
                            },
                            enabled = !busy,
                        ) {
                            Text("Подключиться")
                        }
                    }

                    TextButton(onClick = { mode = "info" }) {
                        Text("Назад")
                    }
                }

                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
            }
        }
    }
}
