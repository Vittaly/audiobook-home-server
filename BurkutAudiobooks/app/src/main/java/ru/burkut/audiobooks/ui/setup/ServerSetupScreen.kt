package ru.burkut.audiobooks.ui.setup

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import kotlinx.coroutines.launch
import ru.burkut.audiobooks.App
import ru.burkut.audiobooks.data.mdns.DiscoveryStatus
import ru.burkut.audiobooks.ui.appInstance
import ru.burkut.audiobooks.ui.viewModelFactoryOf

@Composable
fun ServerSetupScreen(app: App) {
    val vm: ServerDiscoveryViewModel = viewModel(
        factory = viewModelFactoryOf { ServerDiscoveryViewModel(appInstance()) }
    )
    val servers by vm.servers.collectAsState()
    val status by vm.status.collectAsState()
    val scope = rememberCoroutineScope()

    var showManual by remember { mutableStateOf(false) }
    var manualHost by remember { mutableStateOf("") }
    var manualPort by remember { mutableStateOf("8000") }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    DisposableEffect(Unit) {
        vm.start()
        onDispose { vm.stop() }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Burkut Аудиокниги", style = MaterialTheme.typography.headlineMedium)
        Text(
            "Сервер ищется автоматически через mDNS в вашей Wi-Fi сети.",
            style = MaterialTheme.typography.bodyLarge,
        )

        when (status) {
            is DiscoveryStatus.Searching -> Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(Modifier.size(20.dp))
                Spacer(Modifier.size(8.dp))
                Text("Поиск серверов…")
            }
            is DiscoveryStatus.Error -> Text(
                (status as DiscoveryStatus.Error).message,
                color = MaterialTheme.colorScheme.error,
            )
            else -> Unit
        }

        if (servers.isEmpty() && status !is DiscoveryStatus.Searching) {
            Text("Серверы не найдены. Убедитесь, что устройство и сервер подключены к одной сети Wi-Fi.")
        }

        servers.forEach { server ->
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(14.dp),
                ) {
                    Text(server.name, style = MaterialTheme.typography.titleMedium)
                    Text("${server.host}:${server.port}", style = MaterialTheme.typography.bodyMedium)
                    Spacer(Modifier.height(8.dp))
                    Button(
                        onClick = {
                            busy = true
                            connectToServer(
                                app = app,
                                scope = scope,
                                url = server.url,
                                onSuccess = { busy = false },
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

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Button(onClick = { vm.stop(); vm.start() }) {
                Text("Обновить")
            }
            TextButton(onClick = { showManual = !showManual }) {
                Text(if (showManual) "Скрыть" else "Ввести адрес вручную")
            }
        }

        if (showManual) {
            Card(modifier = Modifier.fillMaxWidth()) {
                Column(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(14.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
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
                                onSuccess = { busy = false },
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

        error?.let {
            Text(it, color = MaterialTheme.colorScheme.error)
        }
    }
}
