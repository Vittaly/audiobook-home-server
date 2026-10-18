# План: оформление карточки каталога, фильтры и блок «Содержание»

## Контекст

Отдельного поля «содержание» в БД/API/Compose нет. Сервер сохраняет весь текст
из «Описание» трекера в `release.description` ([`release535.json`](../release535.json:1)),
внутри которого есть встроенный блок вида:

```
...основной текст описания...
 Содержание: 1. Лесной Кот
2. Белый Кречет
3. Хирд
...
```

На экране раздачи ([`ReleaseDetailScreen.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/release/ReleaseDetailScreen.kt:160))
`description` выводится одним `Text` — из-за этого «Содержание» идёт в одну строку с первым пунктом.

Подтверждено пользователем:
- «Содержание» — встроенный блок в `description` на экране раздачи.
- Картинки карточек — квадратные, **без скругления**.

## Изменения

### 1. Карточка списка каталога — квадратная обложка без обрезки кругом
Файл: [`CatalogScreen.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/catalog/CatalogScreen.kt:336)

- В `ReleaseCard` у `AsyncImage` убрать `.clip(CircleShape)` — оставить только `.size(72.dp)`.
  `ContentScale.Crop` уже даёт квадрат.
- Импорт `androidx.compose.foundation.shape.CircleShape` **сохранить** — он ещё нужен
  для точек статуса сервера (строки 80–97).
- Удалить ставший неиспользуемым импорт `androidx.compose.ui.draw.clip` (если больше нигде
  в файле не используется).

### 2. Убрать управление сортировкой
Файл: [`CatalogScreen.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/catalog/CatalogScreen.kt:242)

- Удалить из `CatalogFiltersBar` блок «Сортировка»: `Spacer` + `Text("Сортировка")`
  + `Row` с тремя `FilterChip` (DATE_DESC / TITLE_ASC / YEAR_DESC, строки 242–265).
- Удалить параметр `onSortChange: (CatalogSort) -> Unit` из сигнатуры `CatalogFiltersBar`
  и соответствующий аргумент `onSortChange = vm::setSort` в вызове (строка 144).
- Сортировка остаётся по дате релиза: `CatalogSort.DATE_DESC` — значение по умолчанию
  в [`CatalogViewModel.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/catalog/CatalogViewModel.kt:39).
  Enum `CatalogSort`, `setSort()` и `comparatorFor()` оставить как есть (без риска), UI-контрол
  сортировки больше не показывается.
- Убедиться, что импорт `FilterChip` не остался неиспользуемым (его используют чипы рубрик/серий,
  так что импорт сохраняется).

### 3. Однострочное поле поиска
Файл: [`CatalogScreen.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/catalog/CatalogScreen.kt:223)

- `singleLine = true` уже стоит. Сделать поле компактным однострочным: задать
  `Modifier.fillMaxWidth().heightIn(min = 48.dp)` (или `height(48.dp)`) вместо дефолтной
  высокой высоты `OutlinedTextField`.
- Добавить импорт `androidx.compose.foundation.layout.heightIn` (или `height`).

### 4. Индикатор подключения сервера в шапке — только круг, без надписи
Файл: [`CatalogScreen.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/catalog/CatalogScreen.kt:75)

- Заменить блок статуса в `TopAppBar.actions` (строки 75–105): убрать обе надписи
  («Сервер доступен» / «Оффлайн») и `Row`.
- Если сервер доступен (`isServerOnline == true`) — показать **только яркий зелёный круг**
  (`Surface(shape = CircleShape, color = MaterialTheme.colorScheme.primary, Modifier.size(10.dp))`),
  без текста.
- В автономном режиме — **не показывать ничего** (пусто).
- Пример:

  ```kotlin
  if (isServerOnline) {
      Surface(
          shape = CircleShape,
          color = MaterialTheme.colorScheme.primary, // яркий зелёный
          modifier = Modifier
              .padding(end = 8.dp)
              .size(10.dp),
      ) {}
  }
  ```
- Импорт `CircleShape` сохраняется (используется здесь).
- Проверить, что `Alignment`/`Row` не остались неиспользуемыми (они ещё нужны в других местах файла).

### 5. Блок «Содержание» заголовком, пункты — с новой строки
Файлы: [`Format.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/util/Format.kt:1)
и [`ReleaseDetailScreen.kt`](../BurkutAudiobooks/app/src/main/java/ru/burkut/audiobooks/ui/release/ReleaseDetailScreen.kt:160)

- Добавить в `Format.kt` чистую функцию разбора, например:

  ```kotlin
  data class ParsedDescription(val intro: String, val contents: List<String>)

  fun parseReleaseDescription(description: String?): ParsedDescription? {
      if (description.isNullOrBlank()) return null
      val marker = Regex("(?i)Содержание\\s*:?")
      val m = marker.find(description)
          ?: return ParsedDescription(description.trim(), emptyList())
      val intro = description.substring(0, m.range.first).trim()
      val contents = description.substring(m.range.last + 1)
          .split('\n')
          .map { it.trim().trimStart('-', '·', '•').trim() }
          .filter { it.isNotEmpty() }
      return ParsedDescription(intro, contents)
  }
  ```

- В `ReleaseDetailScreen` заменить прямой вывод `release.description` на:
  - `intro` (если не пустой) — `Text(..., bodySmall, onSurfaceVariant)`;
  - если `contents` не пуст — заголовок `Text("Содержание", style = titleSmall)`;
  - каждый пункт `contents` — отдельным `Text(..., bodySmall)` (с новой строки).

  Это гарантирует: «Содержание» — заголовок, а первая строка перечня начинается с новой строки.

## Проверка

- `cd BurkutAudiobooks && ./gradlew :app:compileDebugKotlin` (Windows: `gradlew.bat`) — сборка без ошибок.
- Визуально: в списке каталога обложки квадратные без скругления; нет переключателя сортировки;
  поиск — компактная одна строка; на экране раздачи «Содержание» — заголовок, пункты — каждый с новой строки.
