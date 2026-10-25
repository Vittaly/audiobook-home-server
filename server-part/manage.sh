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

# Юнит установлен в /etc/systemd/system?
systemd_unit_installed() {
    [[ -f "$SERVICE_FILE" ]]
}

# Юнит установлен и настроен ИМЕННО на этого пользователя/эти пути?
# (защита от шаблона с чужим User=vit и /home/vit/... , из-за которого
#  systemd падает с status=217/USER и уходит в рестарт-петлю)
systemd_unit_matches_host() {
    [[ -f "$SERVICE_FILE" ]] || return 1
    local unit_user unit_exec unit_wd
    unit_user="$(grep -E '^User=' "$SERVICE_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
    unit_exec="$(grep -E '^ExecStart=' "$SERVICE_FILE" 2>/dev/null | head -n1 | cut -d= -f2- | awk '{print $1}' || true)"
    unit_wd="$(grep -E '^WorkingDirectory=' "$SERVICE_FILE" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
    [[ "$unit_user" == "$(id -un)" ]] || return 1
    [[ -n "$unit_wd" && -d "$unit_wd" ]] || return 1
    [[ -n "$unit_exec" && -x "$unit_exec" ]] || return 1
    return 0
}

# Можем ли управлять сервисом через systemd без запроса пароля?
systemd_manageable() {
    [[ "${EUID:-$(id -u)}" -eq 0 ]] && return 0
    sudo -n true 2>/dev/null
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

# Рендерит шаблон юнита под текущего пользователя и пути проекта.
# Аргумент (необязательный) — абсолютный путь к python.
render_unit() {
    local src="$SCRIPT_DIR/deploy/${SERVICE_NAME}.service"
    local py="${1:-$PYTHON}"
    if [[ ! -f "$src" ]]; then
        echo ">> ОШИБКА: не найден $src" >&2
        exit 1
    fi
    sed -e "s|__USER__|$(id -un)|g" \
        -e "s|__HOME__|${HOME:-$SCRIPT_DIR}|g" \
        -e "s|__WORKDIR__|$SCRIPT_DIR|g" \
        -e "s|__PYTHON__|$py|g" \
        "$src"
}

do_install() {
    # systemd требует абсолютный путь в ExecStart
    local py="$PYTHON"
    if [[ "$py" != /* ]]; then
        py="$(command -v "$py" 2>/dev/null || true)"
    fi
    if [[ -z "$py" || ! -x "$py" ]]; then
        echo ">> ОШИБКА: не найден исполняемый Python (venv: $SCRIPT_DIR/.venv)" >&2
        exit 1
    fi
    echo ">> установка $SERVICE_FILE"
    echo ">>   User=$(id -un)"
    echo ">>   WorkingDirectory=$SCRIPT_DIR"
    echo ">>   ExecStart=$py main.py"
    render_unit "$py" | sudo tee "$SERVICE_FILE" >/dev/null
    sudo systemctl daemon-reload
    echo ">> готово. Дальше: ./manage.sh auto  (включить автозапуск)"
}

do_auto() {
    do_install
    sudo systemctl enable --now "$SERVICE_NAME"
    systemctl status --no-pager "$SERVICE_NAME" | head -n 12
}

# --- диспетчер ----------------------------------------------
# Предупреждает, если systemd-юнит есть, но бесполезен/вреден на этой машине.
warn_broken_unit() {
    systemd_unit_installed || return 0
    systemd_unit_matches_host && return 0
    echo ">> !!! systemd-юнит $SERVICE_FILE настроен на другого пользователя/пути"
    echo ">>     (в нём User=$(grep -E '^User=' "$SERVICE_FILE" | head -n1 | cut -d= -f2- || echo '?'), а вы $(id -un)) —"
    echo ">>     systemd падает с status=217/USER и уходит в рестарт-петлю."
    echo ">>     Исправьте:  sudo systemctl disable --now $SERVICE_NAME"
    echo ">>                 ./manage.sh install   (переустановит юнит под текущего пользователя)"
}

case "${1:-}" in
    start)
        if systemd_active; then
            echo ">> сервер уже активен в systemd"
        elif is_systemd && systemd_unit_matches_host && systemd_manageable; then
            do_start_systemd
        else
            if is_systemd; then
                warn_broken_unit
                if systemd_unit_matches_host && ! systemd_manageable; then
                    echo ">> нет доступа к systemd без пароля sudo — запускаю в фоне."
                fi
            fi
            do_start_bg
        fi
        ;;
    stop)
        if systemd_active; then
            do_stop_systemd
        fi
        do_stop_bg
        ;;
    restart)
        if systemd_active; then
            do_stop_systemd
        else
            do_stop_bg
        fi
        if is_systemd && systemd_unit_matches_host && systemd_manageable; then
            do_start_systemd
        else
            if is_systemd; then
                warn_broken_unit
                if systemd_unit_matches_host && ! systemd_manageable; then
                    echo ">> нет доступа к systemd без пароля sudo — запускаю в фоне."
                fi
            fi
            do_start_bg
        fi
        ;;
    status)
        if systemd_active; then
            echo ">> systemd: сервис активен"
            systemctl status --no-pager "$SERVICE_NAME" | head -n 12
        else
            do_status_bg
            if is_systemd; then
                if systemd_unit_installed; then
                    if systemd_unit_matches_host; then
                        echo ">> (systemd-сервис $SERVICE_NAME установлен и корректен, но не активен)"
                    else
                        echo ">> (systemd-сервис $SERVICE_NAME установлен, НО настроен на другого пользователя — см. ./manage.sh install)"
                    fi
                    systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || true
                else
                    echo ">> (systemd-сервис $SERVICE_NAME не установлен)"
                fi
            fi
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