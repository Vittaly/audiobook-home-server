# Адаптация серверной части для headless Linux + внешний диск

## Цель
Добавить поддержку headless Linux **не ломая** существующую Windows-совместимость.

## План работ

### Шаг 1: Обновить `.env` и `.env.example`
Добавить комментарий с примером абсолютного пути для DATA_DIR:
```env
# Может быть относительным (относительно корня проекта) или абсолютным.
# Для внешнего диска укажите абсолютный путь, например:
#   DATA_DIR=/mnt/storage/audiobook-data   (Linux)
#   DATA_DIR=D:\audiobook-data              (Windows)
DATA_DIR=data
```

### Шаг 2: `layout.py` — оставить как есть
Запрещённые символы не мешают Linux, но защищают от проблем при монтировании FAT/NTFS/exFAT-дисков.

### Шаг 3: `daemon.py` — мелкие улучшения для Linux (ничего не ломая на Windows)
- `TransmissionDaemon._candidate_binaries()` — добавить `/usr/local/sbin/transmission-daemon`
- Остальное уже кросс-платформенно

### Шаг 4: `deploy/audiobook-server.service` — systemd unit для Linux
```ini
[Unit]
Description=Audiobook Home Server
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=vit
Group=vit
WorkingDirectory=/home/vit/audiobook-home-server/server-part
ExecStart=/usr/bin/python3 /home/vit/audiobook-home-server/server-part/main.py
Restart=always
RestartSec=10
EnvironmentFile=/home/vit/audiobook-home-server/server-part/.env

[Install]
WantedBy=multi-user.target
```

### Шаг 5: `server-part/manage.sh` — скрипт управления для Linux
Быстрый запуск/остановка/статус без systemd.

### Шаг 6: `.gitignore` — уже игнорирует `data`, дополнительно игнорировать ничего не нужно
Если DATA_DIR — абсолютный путь, он вне проекта и не попадёт в git.

### Шаг 7: Документация
Обновить `server-part/project_description.md` или README:
- Раздел "Настройка внешнего диска" (кросс-платформенно)
- Раздел "Запуск на Linux" (systemd + manage.sh)
- Примечание: Windows-поддержка сохранена

## Кросс-платформенность: что НЕ меняется
- `daemon.py`: все `if _SYSTEM == "Windows"` ветки остаются
- `layout.py`: санитайзер имён остаётся
- Конфигурация (FastAPI, SQLite, пути) — уже ОС-независима