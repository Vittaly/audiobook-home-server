package ru.burkut.audiobooks.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val Brown500 = Color(0xFF8D6E63)
private val Brown700 = Color(0xFF5D4037)
private val Teal200 = Color(0xFF80CBC4)
private val Teal700 = Color(0xFF00796B)

private val LightColors = lightColorScheme(
    primary = Brown700,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFD7CCC8),
    onPrimaryContainer = Color(0xFF3E2723),
    secondary = Teal700,
    onSecondary = Color.White,
    secondaryContainer = Teal200,
    onSecondaryContainer = Color(0xFF004D40),
)

private val DarkColors = darkColorScheme(
    primary = Color(0xFFD7CCC8),
    onPrimary = Brown700,
    primaryContainer = Brown500,
    onPrimaryContainer = Color.White,
    secondary = Teal200,
    onSecondary = Color(0xFF004D40),
    secondaryContainer = Teal700,
    onSecondaryContainer = Color.White,
)

@Composable
fun AppTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = if (isSystemInDarkTheme()) DarkColors else LightColors,
        content = content,
    )
}
