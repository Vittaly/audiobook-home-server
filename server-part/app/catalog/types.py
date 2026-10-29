"""Типы и датаклассы для логики каталога и загрузки.

DownloadResult — единственный тип, который раньше лежал в download.py.
Перенесён сюда, чтобы разорвать циклический импорт:
  book.py -> download.py -> service.py -> db.models (partial) -> ImportError.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class DownloadResult:
    """Результат операции запуска / остановки загрузки.

    Поля:
      success:       успех/неудача
      release_id:    идентификатор раздачи
      book_hash:     хэш книги (строка — первичный ключ Book)
      client_id:     идентификатор торрент-клиента (id торрента или hash)
      status:        новый статус книги или раздачи
      save_path:     путь сохранения файлов
      error_code:    машино-читаемый код: 'not_found'|'no_release'|...
      error_message: человеко-читаемое описание
    """
    success: bool
    release_id: int | None = None
    book_hash: str | None = None
    client_id: int | None = None
    status: str | None = None
    save_path: str | None = None
    error_code: str | None = None       # 'not_found' | 'no_release' | ...
    error_message: str | None = None