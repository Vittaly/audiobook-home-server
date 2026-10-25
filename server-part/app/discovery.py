"""
mDNS-анонс сервиса через AsyncZeroconf.

Публикует _audiobook._tcp.local. с TXT-записями.
start() и stop() — асинхронные, вызываются из lifespan FastAPI.

Определение локального IP (кросс-платформенно):
  1. Если задан MDNS_LOCAL_IP в .env — используем его.
  2. Если установлен psutil — используем его (одинаково на Linux/Windows/macOS).
  3. Платформенные утилиты:
       - Linux:  `ip -j -4 addr show` (iproute2, без внешних зависимостей);
       - Windows: PowerShell `Get-NetAdapter` + `Get-NetIPAddress`;
       - macOS:  `ifconfig -a`.
  4. Если ничего не сработало — fallback на connect(8.8.8.8) (может вернуть VPN IP).

Во всех случаях отбрасываются:
  - loopback и link-local (127.*, 169.254.*);
  - VPN-интерфейсы (wg, tun, tap, wireguard, tailscale, vpn, ...);
  - виртуальные интерфейсы (docker, br-, veth, cni, vEthernet, Hyper-V, ...).
Приоритет выбора: 192.168.* > 10.* > прочие > 172.16.0.0/12 (Docker/VPN).
"""
from __future__ import annotations


import asyncio
import ipaddress
import json
import logging
import platform
import re
import shutil
import socket
import subprocess
from contextlib import suppress

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from app.config import settings

log = logging.getLogger(__name__)


# Ключевые слова для отсеивания VPN-интерфейсов.
# Сравниваются: (а) с началом имени интерфейса, (б) с началом любого
# токена в имени (разделители: пробел, дефис, подчёркивание, скобки).
# Это позволяет матчить и "wg0", и "WireGuard Tunnel",
# и "TAP-Windows Adapter V9", но НЕ ловить "swg0" на "wg".
_VPN_KEYWORDS = (
    "vpn", "tun", "tap", "wg", "wireguard",
    "openvpn", "nord", "express", "proton", "outline",
    "cisco", "anyconnect", "globalprotect", "fortinet",
    "tailscale", "zt", "nebula", "wintun", "tunnel",
    "ppp", "modem",
)

# Виртуальные интерфейсы (Docker, CNI, Hyper-V, VMware, ...).
_VIRTUAL_KEYWORDS = (
    "docker", "br-", "veth", "virbr", "vmnet", "vboxnet",
    "cni", "flannel", "cali", "kube", "lxc", "lxd",
    "vethernet", "hyper-v", "vmware", "virtualbox",
    "loopback", "virtual", "pseudo",
)


class ServiceAnnouncer:
    """Публикует сервис в локальной сети через mDNS/DNS-SD."""

    def __init__(self):
        self._aiozc: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None
        self._service_type: str = ""
        self._service_name: str = ""
        self._local_ip: str = ""
        self._monitor_task: asyncio.Task | None = None

    # ------------------------------------------------------------
    # публикация
    # ------------------------------------------------------------

    async def start(self) -> None:
        log.info(">>> mDNS: старт")
        log.info(">>> mDNS: MDNS_ENABLED=%s", settings.MDNS_ENABLED)
        log.info(">>> mDNS: MDNS_SERVICE_TYPE=%r", settings.MDNS_SERVICE_TYPE)
        log.info(">>> mDNS: API_PORT=%d", settings.API_PORT)
        log.info(">>> mDNS: HEARTBEAT_SEC=%d", settings.MDNS_HEARTBEAT_SEC)
        log.info(">>> mDNS: MDNS_LOCAL_IP=%r", settings.MDNS_LOCAL_IP)

        if not settings.MDNS_ENABLED:
            log.warning(">>> mDNS: выключен (MDNS_ENABLED=false)")
            return

        # --- hostname ---
        try:
            hostname = socket.gethostname()
            log.info(">>> mDNS: hostname=%r", hostname)
        except Exception:
            log.exception(">>> mDNS: не удалось получить hostname")
            return

        # --- локальный IP ---
        local_ip = self._detect_local_ip()
        log.info(">>> mDNS: local_ip=%r", local_ip)
        if not local_ip:
            log.error(">>> mDNS: нет локального IP — публикация невозможна. "
                      "Укажи MDNS_LOCAL_IP в .env.")
            return
        self._local_ip = local_ip

        # --- тип сервиса ---
        service_type = settings.MDNS_SERVICE_TYPE or "_audiobook._tcp.local."
        if not service_type.endswith(".local."):
            service_type = service_type.rstrip(".") + ".local."
        self._service_type = service_type
        log.info(">>> mDNS: тип сервиса=%r", service_type)

        # --- имя сервиса (sanitize под mDNS-метку) ---
        safe_host = self._sanitize_label(hostname)
        service_name = f"audiobook-{safe_host}.{service_type}"
        self._service_name = service_name
        log.info(">>> mDNS: имя сервиса=%r", service_name)

        # --- AsyncServiceInfo ---
        try:
            self._info = AsyncServiceInfo(
                type_=service_type,
                name=service_name,
                addresses=[socket.inet_aton(local_ip)],
                port=settings.API_PORT,
                properties={
                    "version": "1.0",
                    "path": "/",
                    "server_name": hostname,
                },
                server=f"{hostname}.local.",
            )
            log.info(">>> mDNS: AsyncServiceInfo создан")
            log.info(">>> mDNS:   addresses=%s", self._info.parsed_addresses())
            log.info(">>> mDNS:   port=%d", self._info.port)
            log.info(">>> mDNS:   server=%r", self._info.server)
            log.info(">>> mDNS:   properties=%s", self._info.properties)
        except Exception:
            log.exception(">>> mDNS: не удалось создать AsyncServiceInfo")
            return

        # --- AsyncZeroconf ---
        try:
            log.info(">>> mDNS: создаём AsyncZeroconf "
                     "(только интерфейс %s)...", local_ip)
            self._aiozc = AsyncZeroconf(
                ip_version=IPVersion.V4Only,
                interfaces=[local_ip],
            )
            log.info(">>> mDNS: AsyncZeroconf создан")
        except Exception:
            log.exception(">>> mDNS: не удалось создать AsyncZeroconf "
                          "(возможно, порт 5353 занят)")
            await self._cleanup()
            return

        # --- регистрация ---
        try:
            log.info(">>> mDNS: регистрируем сервис...")
            await self._aiozc.async_register_service(self._info)
            log.info(">>> mDNS: сервис зарегистрирован")
        except Exception:
            log.exception(">>> mDNS: не удалось зарегистрировать сервис")
            await self._cleanup()
            return

        # --- верификация (с retry) ---
        await self._verify_visible()

        # --- мониторинг ---
        self._start_monitor()

        log.info(">>> mDNS: публикация завершена")

    # ------------------------------------------------------------
    # остановка
    # ------------------------------------------------------------

    async def stop(self) -> None:
        log.info(">>> mDNS: остановка сервиса")

        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
            log.info(">>> mDNS: монитор остановлен")

        await self._cleanup()
        log.info(">>> mDNS: сервис снят с публикации")

    # ------------------------------------------------------------
    # мониторинг
    # ------------------------------------------------------------

    def _start_monitor(self) -> None:
        if settings.MDNS_HEARTBEAT_SEC <= 0:
            log.info(">>> mDNS: мониторинг выключен (HEARTBEAT_SEC=0)")
            return

        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="mdns-monitor",
        )
        log.info(">>> mDNS: мониторинг запущен "
                 "(интервал %d сек)", settings.MDNS_HEARTBEAT_SEC)

    async def _monitor_loop(self) -> None:
        interval = settings.MDNS_HEARTBEAT_SEC
        try:
            while True:
                await asyncio.sleep(interval)
                await self._heartbeat()
        except asyncio.CancelledError:
            log.debug(">>> mDNS: monitor loop cancelled")
            raise

    async def _heartbeat(self) -> None:
        if self._aiozc is None or self._info is None:
            log.warning(">>> mDNS: пульс — zeroconf потерян")
            return

        try:
            probe = AsyncServiceInfo(self._service_type, self._service_name)
            ok = await probe.async_request(self._aiozc.zeroconf, timeout=2000)
        except Exception:
            log.exception(">>> mDNS: пульс — ошибка запроса")
            return

        if not ok:
            log.warning(">>> mDNS: пульс — сервис НЕ виден в сети. "
                        "Возможен firewall или смена интерфейса.")
            return

        addrs = probe.parsed_addresses()
        if not addrs:
            log.warning(">>> mDNS: пульс — сервис виден, но без A-записи "
                        "(клиенты не смогут подключиться)")
            return

        log.info(">>> mDNS: пульс — сервис виден, addresses=%s, port=%d",
                 addrs, probe.port)

    # ------------------------------------------------------------
    # внутреннее
    # ------------------------------------------------------------

    async def _verify_visible(self) -> None:
        """Сразу после регистрации запись ещё не разлетелась — делаем retry."""
        last_warn = False
        for attempt in range(3):
            await asyncio.sleep(0.5)
            try:
                probe = AsyncServiceInfo(self._service_type, self._service_name)
                ok = await probe.async_request(self._aiozc.zeroconf, timeout=2000)
            except Exception:
                log.exception(">>> mDNS: ошибка верификации (попытка %d/3)",
                              attempt + 1)
                continue

            if ok:
                addrs = probe.parsed_addresses()
                if addrs:
                    log.info(">>> mDNS: ✓ сервис виден в сети")
                    log.info(">>> mDNS:   addresses=%s", addrs)
                    log.info(">>> mDNS:   port=%d", probe.port)
                    return
                log.warning(">>> mDNS: сервис найден, но без адресов "
                            "(попытка %d/3)", attempt + 1)
                last_warn = True
            else:
                log.warning(">>> mDNS: обратный запрос не нашёл сервис "
                            "(попытка %d/3)", attempt + 1)
                last_warn = True

        if last_warn:
            log.warning(">>> mDNS: сервис зарегистрирован, но не подтверждён "
                        "обратным запросом. Возможен firewall или изоляция "
                        "multicast (Docker bridge, VPN).")

    async def _cleanup(self) -> None:
        if self._aiozc is not None and self._info is not None:
            try:
                log.info(">>> mDNS: unregister_service...")
                await self._aiozc.async_unregister_service(self._info)
            except Exception:
                log.exception(">>> mDNS: ошибка async_unregister_service")

        if self._aiozc is not None:
            try:
                log.info(">>> mDNS: закрываем AsyncZeroconf...")
                await self._aiozc.async_close()
            except Exception:
                log.exception(">>> mDNS: ошибка async_close")

        self._aiozc = None
        self._info = None

    @staticmethod
    def _sanitize_label(name: str, max_len: int = 40) -> str:
        """Приводит hostname к валидной mDNS-метке (a-z0-9-)."""
        name = name.strip().lower()
        name = re.sub(r"[^a-z0-9\-]", "-", name)
        name = re.sub(r"-+", "-", name).strip("-")
        if not name:
            name = "host"
        return name[:max_len]

    # ------------------------------------------------------------
    # определение локального IP
    # ------------------------------------------------------------

    @staticmethod
    def _matches(name: str, keywords: tuple[str, ...]) -> bool:
        """
        Проверяет, начинается ли имя интерфейса или любой его токен
        с одного из ключевых слов. Устойчиво к "wg0" vs "swg0",
        и работает для "WireGuard Tunnel", "vEthernet (Default Switch)",
        "TAP-Windows Adapter V9".
        """
        name_lower = name.lower()
        if any(name_lower.startswith(k) for k in keywords):
            return True
        tokens = re.split(r"[\s\-_()\[\]]+", name_lower)
        return any(t.startswith(k) for t in tokens for k in keywords)

    @staticmethod
    def _pick_best(candidates: list[tuple[str, str, bool]]) -> str | None:
        """
        Общий фильтр/сортировка кандидатов (одинаково для всех ОС).
        Приоритет: 192.168.* > 10.* > прочие > 172.16.0.0/12 (Docker/VPN).
        """
        if not candidates:
            return None

        with_bcast = [c for c in candidates if c[2]]
        pool = with_bcast if with_bcast else candidates

        def priority(item: tuple[str, str, bool]) -> int:
            ip = item[1]
            if ip.startswith("192.168."):
                return 0
            if ip.startswith("10."):
                return 1
            try:
                if ipaddress.ip_address(ip) in ipaddress.ip_network("172.16.0.0/12"):
                    return 3
            except ValueError:
                pass
            return 2

        pool.sort(key=priority)
        iface, ip, has_bcast = pool[0]

        log.info(">>> mDNS: выбран iface=%r ip=%s (broadcast=%s)",
                 iface, ip, has_bcast)
        log.info(">>> mDNS: всего кандидатов: %s",
                 [(c[0], c[1]) for c in candidates])
        return ip

    @staticmethod
    def _detect_local_ip() -> str | None:
        # --- ручной override ---
        if settings.MDNS_LOCAL_IP:
            log.info(">>> mDNS: IP из .env (MDNS_LOCAL_IP): %s",
                     settings.MDNS_LOCAL_IP)
            return settings.MDNS_LOCAL_IP

        system = platform.system()
        log.info(">>> mDNS: автоопределение IP (platform=%s)", system)

        # 1) psutil — кросс-платформенно, без парсинга вывода утилит
        ip = ServiceAnnouncer._ip_via_psutil()
        if ip:
            log.info(">>> mDNS: IP через psutil: %s", ip)
            return ip

        # 2) платформенные утилиты
        if system == "Linux":
            ip = ServiceAnnouncer._ip_via_linux()
        elif system == "Windows":
            ip = ServiceAnnouncer._ip_via_windows()
        elif system == "Darwin":
            ip = ServiceAnnouncer._ip_via_macos()
        else:
            ip = None

        if ip:
            return ip

        # 3) аварийный резерв
        return ServiceAnnouncer._fallback_ip()

    # ---- Linux ----

    @staticmethod
    def _ip_via_linux() -> str | None:
        if not shutil.which("ip"):
            log.warning(">>> mDNS: `ip` не найден (Linux)")
            return None
        try:
            out = subprocess.check_output(
                ["ip", "-j", "-4", "addr", "show"],
                text=True, timeout=3,
            )
            interfaces = json.loads(out)
        except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
            log.warning(">>> mDNS: `ip -j` не сработал: %s", exc)
            return None

        candidates: list[tuple[str, str, bool]] = []
        for iface in interfaces:
            name = iface.get("ifname", "")
            if not name or name == "lo":
                continue
            flags = iface.get("flags") or []
            if "UP" not in flags:
                log.debug(">>> mDNS: %s — down, пропуск", name)
                continue
            if ServiceAnnouncer._matches(name, _VPN_KEYWORDS):
                log.info(">>> mDNS: %s — VPN по имени, пропуск", name)
                continue
            if ServiceAnnouncer._matches(name, _VIRTUAL_KEYWORDS):
                log.info(">>> mDNS: %s — виртуальный, пропуск", name)
                continue

            has_bcast = "BROADCAST" in flags

            for addr in iface.get("addr_info", []):
                if addr.get("family") != "inet":
                    continue
                ip = addr.get("local")
                if not ip or ip.startswith(("127.", "169.254.")):
                    continue
                try:
                    if not ipaddress.ip_address(ip).is_private:
                        continue
                except ValueError:
                    continue
                candidates.append((name, ip, has_bcast))
                log.debug(">>> mDNS: кандидат iface=%s ip=%s bcast=%s",
                          name, ip, has_bcast)

        return ServiceAnnouncer._pick_best(candidates)

    # ---- Windows ----

    _PS_DETECT = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$adapters = Get-NetAdapter -ErrorAction SilentlyContinue |
    Where-Object { $_.Status -eq 'Up' }
$rows = foreach ($a in $adapters) {
    $ips = Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 `
           -ErrorAction SilentlyContinue
    foreach ($ip in $ips) {
        [PSCustomObject]@{
            iface = $a.Name
            desc  = $a.InterfaceDescription
            ip    = $ip.IPAddress
        }
    }
}
if ($rows) { $rows | ConvertTo-Json -Compress }
"""

    @staticmethod
    def _ip_via_windows() -> str | None:
        ps = shutil.which("powershell") or shutil.which("pwsh")
        if not ps:
            log.warning(">>> mDNS: PowerShell не найден (Windows)")
            return None

        try:
            out = subprocess.check_output(
                [ps, "-NoProfile", "-NonInteractive", "-Command",
                 ServiceAnnouncer._PS_DETECT],
                text=True, timeout=8, encoding="utf-8",
            )
        except subprocess.SubprocessError as exc:
            log.warning(">>> mDNS: PowerShell не сработал: %s", exc)
            return None

        if not out.strip():
            log.warning(">>> mDNS: PowerShell вернул пустой список адаптеров")
            return None

        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            log.warning(">>> mDNS: не удалось разобрать JSON PowerShell: %s",
                        exc)
            return None

        if isinstance(data, dict):
            data = [data]

        candidates: list[tuple[str, str, bool]] = []
        for row in data:
            name = row.get("iface") or ""
            desc = row.get("desc") or ""
            ip = row.get("ip") or ""
            combined = f"{name} {desc}".strip()
            if not ip or ip.startswith(("127.", "169.254.")):
                continue
            if ServiceAnnouncer._matches(combined, _VPN_KEYWORDS):
                log.info(">>> mDNS: %s — VPN по имени, пропуск", combined)
                continue
            if ServiceAnnouncer._matches(combined, _VIRTUAL_KEYWORDS):
                log.info(">>> mDNS: %s — виртуальный, пропуск", combined)
                continue
            try:
                if not ipaddress.ip_address(ip).is_private:
                    continue
            except ValueError:
                continue
            # Get-NetIPAddress не отдаёт broadcast-флаг. После фильтра
            # по имени оставшиеся адаптеры — реальные LAN-кандидаты.
            candidates.append((combined, ip, True))

        return ServiceAnnouncer._pick_best(candidates)

    # ---- macOS ----

    @staticmethod
    def _ip_via_macos() -> str | None:
        if not shutil.which("ifconfig"):
            log.warning(">>> mDNS: `ifconfig` не найден (macOS)")
            return None
        try:
            out = subprocess.check_output(
                ["ifconfig", "-a"], text=True, timeout=3,
            )
        except subprocess.SubprocessError as exc:
            log.warning(">>> mDNS: ifconfig не сработал: %s", exc)
            return None

        candidates: list[tuple[str, str, bool]] = []
        current: str | None = None
        has_bcast = False

        for line in out.splitlines():
            if line and not line[0].isspace() and ":" in line:
                current = line.split(":", 1)[0]
                has_bcast = False
                if ServiceAnnouncer._matches(
                    current, _VPN_KEYWORDS + _VIRTUAL_KEYWORDS
                ):
                    current = None
                continue

            if current is None:
                continue

            parts = line.split()
            if len(parts) >= 2 and parts[0] == "inet":
                ip = parts[1]
                if ip.startswith(("127.", "169.254.")):
                    continue
                has_bcast = "broadcast" in parts
                try:
                    if not ipaddress.ip_address(ip).is_private:
                        continue
                except ValueError:
                    continue
                candidates.append((current, ip, has_bcast))

        return ServiceAnnouncer._pick_best(candidates)

    # ---- psutil (опциональный) ----

    @staticmethod
    def _ip_via_psutil() -> str | None:
        try:
            import psutil
        except ImportError:
            return None

        try:
            addrs_map = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
        except Exception as exc:
            log.warning(">>> mDNS: psutil не смог прочитать интерфейсы: %s", exc)
            return None

        candidates: list[tuple[str, str, bool]] = []
        for iface, addrs in addrs_map.items():
            st = stats.get(iface)
            if st is None or not st.isup:
                continue
            if ServiceAnnouncer._matches(iface, _VPN_KEYWORDS):
                log.info(">>> mDNS: %s — VPN по имени, пропуск", iface)
                continue
            if ServiceAnnouncer._matches(iface, _VIRTUAL_KEYWORDS):
                log.info(">>> mDNS: %s — виртуальный, пропуск", iface)
                continue

            has_bcast = any(
                a.family == socket.AF_INET and a.broadcast
                for a in addrs
            )

            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                ip = a.address
                if ip.startswith(("127.", "169.254.")):
                    continue
                try:
                    if not ipaddress.ip_address(ip).is_private:
                        continue
                except ValueError:
                    continue
                candidates.append((iface, ip, has_bcast))
                log.debug(">>> mDNS: кандидат iface=%s ip=%s bcast=%s",
                          iface, ip, has_bcast)

        return ServiceAnnouncer._pick_best(candidates)

    # ---- fallback ----

    @staticmethod
    def _fallback_ip() -> str | None:
        """
        Последний резерв — connect(8.8.8.8).
        Может вернуть VPN/Docker IP, если default route уходит туда.
        Используется только когда ни psutil, ни платформенная утилита
        не дали результата.
        """
        log.warning(">>> mDNS: fallback на connect-метод "
                    "(может вернуть VPN IP)")
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.settimeout(2)
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                log.info(">>> mDNS: fallback выбрал IP=%s", ip)
            finally:
                s.close()

            if ip.startswith("127."):
                log.warning(">>> mDNS: IP loopback (%s)", ip)
                return None
            if ip.startswith("169.254."):
                log.warning(">>> mDNS: IP link-local (%s)", ip)
                return None

            return ip
        except Exception:
            log.exception(">>> mDNS: fallback не сработал")
            return None