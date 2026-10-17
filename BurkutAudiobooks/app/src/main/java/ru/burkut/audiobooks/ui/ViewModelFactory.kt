package ru.burkut.audiobooks.ui

import android.app.Application
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelProvider.AndroidViewModelFactory.Companion.APPLICATION_KEY
import androidx.lifecycle.viewmodel.CreationExtras
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import ru.burkut.audiobooks.App

/**
 * Универсальная фабрика ViewModel без Hilt.
 *
 * Application достаётся из [CreationExtras] — официальный механизм AndroidX,
 * работает и в Activity, и в Fragment, и в Compose через `viewModel(factory = ...)`.
 *
 * Пример:
 * ```
 * val vm: PlayerViewModel = viewModel(
 *     factory = viewModelFactoryOf { app -> PlayerViewModel(app) }
 * )
 * ```
 */
inline fun <reified VM : ViewModel> viewModelFactoryOf(
    crossinline creator: (App) -> VM,
): ViewModelProvider.Factory = viewModelFactory {
    initializer {
        val app = requireNotNull(this[APPLICATION_KEY]) {
            "Application отсутствует в CreationExtras"
        } as App
        creator(app)
    }
}

/**
 * Перегрузка, если вью-модели достаточно базового [Application].
 */
inline fun <reified VM : ViewModel> viewModelFactoryOfApp(
    crossinline creator: (Application) -> VM,
): ViewModelProvider.Factory = viewModelFactory {
    initializer {
        val app = requireNotNull(this[APPLICATION_KEY]) {
            "Application отсутствует в CreationExtras"
        } as Application
        creator(app)
    }
}

/**
 * Фабрика для случаев, когда [CreationExtras] недоступны
 * (ручное создание VM в тестах или legacy-коде).
 * Приложение передаётся явно.
 */
fun <VM : ViewModel> viewModelFactoryOf(
    app: App,
    creator: (App) -> VM,
): ViewModelProvider.Factory = object : ViewModelProvider.Factory {
    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(modelClass: Class<T>): T = creator(app) as T

    @Suppress("UNCHECKED_CAST")
    override fun <T : ViewModel> create(
        modelClass: Class<T>,
        extras: CreationExtras,
    ): T = creator(app) as T
}

/**
 * Глобальный доступ к [App] из любого места процесса.
 *
 * Возвращает тот же объект, что `application` в Activity /
 * `LocalContext.current.applicationContext`. Инициализируется в [App.onCreate]
 * первой строкой — до любого обращения из UI.
 */
fun appInstance(): App = App.instance