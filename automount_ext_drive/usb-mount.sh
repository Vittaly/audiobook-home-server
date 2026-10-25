#!/usr/bin/env bash
set -euo pipefail

LOG_TAG="usb-automount"
MOUNT_BASE="/media"
ENV_DIR="/run/usb-mount"

# ВАЖНО: uid/gid первого пользователя Armbian. Проверить: id -u; id -g
TARGET_UID=1000
TARGET_GID=1000

log() { logger -t "${LOG_TAG}" -p user.info "$*"; }

compute_mount_name() {
    local dev_path="/dev/${DEVICE}"
    local label uuid
    label=$(blkid -s LABEL -o value "$dev_path" 2>/dev/null || true)
    uuid=$(blkid -s UUID  -o value "$dev_path" 2>/dev/null || true)

    if [[ -n "$label" ]]; then
        local translit="$label"
        if command -v uconv >/dev/null 2>&1; then
            translit=$(printf '%s' "$label" \
                | uconv -x 'Russian-Latin/BGN; Latin-ASCII' 2>/dev/null \
                || printf '%s' "$label")
        fi
        local safe
        safe=$(printf '%s' "$translit" | tr -c 'A-Za-z0-9._-' '_' | sed 's/_*$//')
        if [[ "$safe" =~ [A-Za-z0-9] ]]; then
            printf '%s' "$safe"
            return 0
        fi
        log "WARNING: LABEL '$label' unusable after translit, falling back to UUID"
    fi

    if [[ -n "$uuid" ]]; then
        printf '%s' "$(printf '%s' "$uuid" | tr -d '-' | cut -c1-4 | tr 'A-F' 'a-f')"
        return 0
    fi

    log "WARNING: neither LABEL nor UUID for $dev_path, using device name"
    printf '%s' "$DEVICE"
}

do_resolve() {
    local dev_path="/dev/${DEVICE}"
    local fstype
    fstype=$(blkid -s TYPE -o value "$dev_path" 2>/dev/null || true)
    if [[ -z "$fstype" ]]; then
        log "ERROR: no fs type for $dev_path"
        exit 1
    fi

    local name; name=$(compute_mount_name)
    local mount_point="${MOUNT_BASE}/${name}"

    # Обработка коллизий: два диска с одинаковой меткой
    local n=1
    while mountpoint -q "$mount_point" 2>/dev/null || \
          { [[ -d "$mount_point" ]] && [[ -n "$(ls -A "$mount_point" 2>/dev/null)" ]]; }; do
        mount_point="${MOUNT_BASE}/${name}-${n}"
        n=$((n + 1))
        if (( n > 50 )); then
            log "ERROR: cannot find free mount point for ${name}"
            exit 1
        fi
    done

    local opts="defaults,nofail"
    case "$fstype" in
        vfat|exfat|ntfs|ntfs3)
            opts+=",uid=${TARGET_UID},gid=${TARGET_GID},umask=000"
            ;;
        ext4|ext3|ext2)
            # Для ext* права управляются владельцем файлов, а не mount-опциями.
            # При необходимости сделайте chown на точке после монтирования вручную.
            ;;
        *)
            log "WARNING: unknown fs '$fstype', using defaults"
            ;;
    esac

    mkdir -p "$ENV_DIR"
    cat > "${ENV_DIR}/${DEVICE}.env" <<EOF
DEV_PATH=${dev_path}
MOUNT_POINT=${mount_point}
MOUNT_OPTS=${opts}
EOF
    log "Resolved ${DEVICE} → ${mount_point} (opts: ${opts})"
}

do_cleanup() {
    local env_file="${ENV_DIR}/${DEVICE}.env"
    local mount_point=""
    if [[ -r "$env_file" ]]; then
        mount_point=$(grep -E '^MOUNT_POINT=' "$env_file" | cut -d= -f2-)
    fi
    if [[ -n "$mount_point" ]]; then
        rmdir "$mount_point" 2>/dev/null || true
    fi
    rm -f "$env_file"
    log "Cleaned up ${DEVICE}"
}

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 {resolve|cleanup} <device>" >&2
    exit 1
fi
DEVICE="$2"

case "${1:-}" in
    resolve) do_resolve ;;
    cleanup) do_cleanup ;;
    *) echo "Usage: $0 {resolve|cleanup} <device>" >&2; exit 1 ;;
esac
