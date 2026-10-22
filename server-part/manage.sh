#!/usr/bin/env bash
# ============================================================
# Audiobook Home Server — управление запуском (Linux, headless)
# ============================================================
#
# Использование:
#   ./manage.sh start      — запустить сервер в фоне
#   ./manage.sh stop       — остановить
#   ./manage.sh restart    — перезапустить
#   ./manage.sh status     — статус (PID, uptime, порт)
#   ./manage.sh logs       — хвост лога
#   ./manage.sh install    — установить/обновить systemd-юнит
#   ./manage.sh auto       — включить автозапуск через systemd
#
# Если systemd доступен — скрипт использует его (рекомендуется).
# Иначе — запускает процесс в фоне через nohup с PID-файлом.
# ============================================================

set -euo pipefail

# --- пути ---------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Автоопределение python из venv (если есть).
# Приоритет (снизу вверх переопределяет PYTHON):
#   1. $PYTHON        — переменная окружения (самый высокий)
#   2. .venv/bin/python3  — venv в корне проекта (если существует)
#   3. ../.venv/bin/python3 — venv в родительском каталоге
#   4. python3        — системный (если ничего не найдено)
if [[ -z "${PYTHON:-}" ]]; then
    if [[ -x "$SCRIPT_DIR/.venv/bin/python3" ]]; then
        PYTHON="$SCRIPT_DIR/.venv/bin/python3"
    elif [[ -x "$(dirname "$SCRIPT_DIR")/.venv/bin/python3" ]]; then
        PYTHON="$(dirname "$SCRIPT_DIR")/.venv/bin/python3"
    else
        PYTHON="python3"
    fi
fi
export PYTHON

SERVICE_NAME="audiobook-server"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_NAME="app.log"

# --- вспомогательные функции --------------------------------
find_data_dir() {
    # читает DATA_DIR из .env (уважает абсолютные пути)
    local val
    val="$(grep -E '^DATA_DIR=' .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' "')"
    if [[ -z "$val" ]]; then
        echo "$SCRIPT_DIR/data"
    elif [[ "$val" == /* ]]; then
        echo "$val"
    else
        echo "$SCRIPT_DIR/$val"
    fi
}

log_file() {
    local dd
    dd="$(find_data_dir)"
    echo "$dd/$LOG_NAME"
}

pid_file() {
    local dd
    dd="$(find_data_dir)"
    echo "$dd/server.pid"
}

is_systemd() {
    command -v systemctl >/dev/null 2>&1 && [[ "$(ps -p 1 -o comm= 2>/dev/null || true)" == "systemd" ]]
}

systemd_active() {
    is_systemd && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

# --- действия -----------------------------------------------
do_start_systemd() {
    echo ">> systemd: запуск $SERVICE_NAME"
    sudo systemctl start "$SERVICE_NAME"
    systemctl status --no-pager "$SERVICE_NAME" | head -n 12
}

do_start_bg() {
    if [[ -f "$(pid_file)" ]] && kill -0 "$(cat "$(pid_file)")" 2>/dev/null; then
        echo ">> сервер уже запущен, PID=$(cat "$(pid_file)")"
        return 0
    fi
    echo ">> запуск в фоне (без systemd)..."
    # создаём папку для лога (сервер создаёт остальные сам)
    mkdir -p "$(dirname "$(log_file)")"
    nohup "$PYTHON" main.py >>"$(log_file)" 2>&1 &
    echo $! > "$(pid_file)"
    echo ">> PID=$(cat "$(pid_file)"), лог: $(log_file)"
    sleep 2
    do_status_bg
}

do_stop_systemd() {
    echo ">> systemd: остановка $SERVICE_NAME"
    sudo systemctl stop "$SERVICE_NAME"
}

do_stop_bg() {
    local pf pid
    pf="$(pid_file)"
    if [[ ! -f "$pf" ]]; then
        echo ">> PID-файл не найден — сервер не запущен через скрипт"
        return 0
    fi
    pid="$(cat "$pf")"
    if kill -0 "$pid" 2>/dev/null; then
        echo ">> остановка PID=$pid (SIGTERM)..."
        kill -TERM "$pid"
        for _ in $(seq 1 30); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 1
        done
        kill -0 "$pid" 2>/dev/null && { echo ">> не остановился, SIGKILL"; kill -9 "$pid" || true; }
    else
        echo ">> процесс $pid уже не работает"
    fi
    rm -f "$pf"
}

do_status_bg() {
    local pf pid dd port
    pf="$(pid_file)"
    dd="$(find_data_dir)"
    port="$(grep -E '^API_PORT=' .env 2>/dev/null | head -n1 | cut -d= -f2- | tr -d ' ' || echo 8000)"
    echo ">> data_dir: $dd"
    echo ">> api_port: ${port:-8000}"
    if [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null; then
        pid="$(cat "$pf")"
        echo ">> сервер: РАБОТАЕТ (PID=$pid), uptime: $(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')"
    else
        echo ">> сервер: не запущен"
    fi
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS "http://127.0.0.1:${port:-8000}/health" >/dev/null 2>&1; then
            echo ">> health: OK"
        else
            echo ">> health: недоступен"
        fi
    fi
}

do_install() {
    local src="$SCRIPT_DIR/deploy/${SERVICE_NAME}.service"
    if [[ ! -f "$src" ]]; then
        echo ">> ОШИБКА: не найден $src" >&2
        exit 1
    fi
    echo ">> установка $SERVICE_FILE из $src"
    sudo cp "$src" "$SERVICE_FILE"
    sudo systemctl daemon-reload
    echo ">> готово. Дальше: ./manage.sh auto  (включить автозапуск)"
}

do_auto() {
    do_install
    sudo systemctl enable --now "$SERVICE_NAME"
    systemctl status --no-pager "$SERVICE_NAME" | head -n 12
}

# --- диспетчер ----------------------------------------------
case "${1:-}" in
    start)
        if systemd_active; then
            echo ">> сервер уже активен в systemd"
        elif is_systemd; then
            do_start_systemd
        else
            do_start_bg
        fi
        ;;
    stop)
        if systemd_active; then
            do_stop_systemd
        else
            do_stop_bg
        fi
        ;;
    restart)
        if systemd_active; then
            do_stop_systemd
            do_start_systemd
        else
            do_stop_bg
            do_start_bg
        fi
        ;;
    status)
        if systemd_active; then
            echo ">> systemd: сервис активен"
            systemctl status --no-pager "$SERVICE_NAME" | head -n 12
        elif is_systemd; then
            do_status_bg
            echo ">> (systemd-сервис $SERVICE_NAME не установлен/не активен)"
            systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || true
        else
            do_status_bg
        fi
        ;;
    logs)
        tail -n 100 -f "$(log_file)"
        ;;
    install)
        do_install
        ;;
    auto)
        do_auto
        ;;
    *)
        echo "Использование: $0 {start|stop|restart|status|logs|install|auto}"
        exit 1
        ;;
esac