package ru.burkut.audiobooks.ui.components

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import coil.compose.AsyncImage
import ru.burkut.audiobooks.data.api.BookOut
import ru.burkut.audiobooks.ui.util.formatSeconds
import ru.burkut.audiobooks.ui.util.label
import ru.burkut.audiobooks.ui.util.rememberBaseUrl

@Composable
fun BookListItem(
    book: BookOut,
    onClick: () -> Unit,
) {
    val baseUrl = rememberBaseUrl()
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 6.dp)
            .clickable(onClick = onClick),
    ) {
        Row(modifier = Modifier.padding(10.dp)) {
            AsyncImage(
                model = book.cover_url?.let { url ->
                    if (url.startsWith("http")) url else baseUrl?.trimEnd('/')?.plus(
                        if (url.startsWith("/")) url else "/$url",
                    )
                },
                contentDescription = null,
                contentScale = androidx.compose.ui.layout.ContentScale.Crop,
                modifier = Modifier
                    .size(64.dp)
                    .clip(RoundedCornerShape(4.dp)),
            )
            Spacer(Modifier.width(12.dp))
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    book.title,
                    style = MaterialTheme.typography.titleMedium,
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                book.author?.let {
                    Text(
                        it,
                        style = MaterialTheme.typography.bodyMedium,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                Spacer(Modifier.height(4.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Surface(
                        shape = RoundedCornerShape(4.dp),
                        color = MaterialTheme.colorScheme.secondaryContainer,
                    ) {
                        Text(
                            book.status.label(),
                            style = MaterialTheme.typography.labelSmall,
                            modifier = Modifier.padding(horizontal = 6.dp, vertical = 2.dp),
                        )
                    }
                    Spacer(Modifier.width(8.dp))
                    book.year?.let { Text("$it") }
                    book.total_duration_sec?.let {
                        Text(" · ${formatSeconds(it)}")
                    }
                    Spacer(Modifier.weight(1f))
                    Text(
                        "${book.chapters_ready}/${book.chapters_count} глав",
                        style = MaterialTheme.typography.labelMedium,
                    )
                }
            }
        }
    }
}
