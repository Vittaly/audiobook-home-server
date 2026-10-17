package ru.burkut.audiobooks.data.settings

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

class Settings(context: Context) {
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    private val _baseUrl = MutableStateFlow(prefs.getString(KEY_BASE_URL, null))
    val baseUrl: StateFlow<String?> = _baseUrl.asStateFlow()

    init {
        prefs.registerOnSharedPreferenceChangeListener { _, key ->
            if (key == KEY_BASE_URL) _baseUrl.value = prefs.getString(KEY_BASE_URL, null)
        }
    }

    fun baseUrlOrNull(): String? = prefs.getString(KEY_BASE_URL, null)

    fun saveUrl(url: String) {
        val normalized = if (url.endsWith("/")) url else "$url/"
        prefs.edit().putString(KEY_BASE_URL, normalized).apply()
    }

    fun saveServer(host: String, port: Int) {
        saveUrl("http://$host:$port/")
    }

    fun clearServer() {
        prefs.edit().remove(KEY_BASE_URL).apply()
    }

    companion object {
        private const val PREFS = "settings"
        private const val KEY_BASE_URL = "base_url"
    }
}
