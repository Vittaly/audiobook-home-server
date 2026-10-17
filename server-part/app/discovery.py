"""
mDNS-анонс сервиса через AsyncZeroconf.

Публикует _audiobook._tcp.local. с TXT-записями.
start() и stop() — асинхронные, вызываются из lifespan FastAPI.

Определение локального IP:
  1. Если задан MDNS_LOCAL_IP в .env — используем его.
  2. Иначе перебираем интерфейсы через psutil:
     - исключаем VPN по имени (vpn, tun, tap, wg, wireguard, ...);
     - исключаем loopback и link-local;
     - предпочитаем интерфейсы с broadcast (реальная локалка);
     - приоритет: 192.168.* > 10.* > остальные.
  3. Если psutil недоступен — fallback на connect(8.8.8.8).
"""

import asyncio
import ipaddress
import logging
import socket
from contextlib import suppress

from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from app.config import settings

log = logging.getLogger(__name__)

# Ключевые слова для отсеивания VPN-интерфейсов по имени.
# Проверяются в нижнем регистре.
_VPN_KEYWORDS = (
    "vpn", "tun", "tap", "wg", "wireguard",
    "openvpn", "nord", "express", "proton", "outline",
    "cisco", "anyconnect", "globalprotect", "fortinet",
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

        # --- имя сервиса ---
        safe_host = hostname.replace(".", "-").replace(" ", "-")
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
                interfaces=[local_ip],       # ← единственный интерфейс
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

        # --- верификация ---
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

        log.info(">>> mDNS: пульс — сервис виден, addresses=%s, port=%d",
                 probe.parsed_addresses(), probe.port)

    # ------------------------------------------------------------
    # внутреннее
    # ------------------------------------------------------------

    async def _verify_visible(self) -> None:
        try:
            probe = AsyncServiceInfo(self._service_type, self._service_name)
            ok = await probe.async_request(self._aiozc.zeroconf, timeout=2000)
            if ok:
                log.info(">>> mDNS: ✓ сервис виден в сети")
                log.info(">>> mDNS:   addresses=%s", probe.parsed_addresses())
                log.info(">>> mDNS:   port=%d", probe.port)
            else:
                log.warning(">>> mDNS: сервис зарегистрирован, но не найден "
                            "при обратном запросе. Возможен firewall.")
        except Exception:
            log.exception(">>> mDNS: ошибка верификации (не критично)")

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

    # ------------------------------------------------------------
    # определение локального IP
    # ------------------------------------------------------------

    @staticmethod
    def _detect_local_ip() -> str | None:
        """
        Определяет IP, видимый в локальной сети.
        Перебирает все интерфейсы, исключает VPN, loopback и link-local.
        """
        # --- ручной override ---
        if settings.MDNS_LOCAL_IP:
            log.info(">>> mDNS: IP из .env (MDNS_LOCAL_IP): %s",
                     settings.MDNS_LOCAL_IP)
            return settings.MDNS_LOCAL_IP

        log.info(">>> mDNS: автоопределение IP через psutil...")

        try:
            import psutil
        except ImportError:
            log.warning(">>> mDNS: psutil не установлен, "
                        "используем fallback")
            return ServiceAnnouncer._fallback_ip()

        try:
            interfaces = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
        except Exception:
            log.exception(">>> mDNS: psutil не смог получить интерфейсы")
            return ServiceAnnouncer._fallback_ip()

        candidates: list[tuple[str, str, bool]] = []  # (iface, ip, has_broadcast)

        for iface, addrs in interfaces.items():
            stat = stats.get(iface)
            if stat is None or not stat.isup:
                log.debug(">>> mDNS: %s — down, пропуск", iface)
                continue

            iface_lower = iface.lower()
            if any(k in iface_lower for k in _VPN_KEYWORDS):
                log.info(">>> mDNS: %s — VPN по имени, пропуск", iface)
                continue

            has_broadcast = any(
                a.family == socket.AF_INET and a.broadcast
                for a in addrs
            )

            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                ip = addr.address

                if ip.startswith("127."):
                    continue
                if ip.startswith("169.254."):
                    continue

                try:
                    ip_obj = ipaddress.ip_address(ip)
                    if not ip_obj.is_private:
                        continue
                except ValueError:
                    continue

                candidates.append((iface, ip, has_broadcast))
                log.debug(">>> mDNS: кандидат iface=%s ip=%s bcast=%s",
                          iface, ip, has_broadcast)

        if not candidates:
            log.warning(">>> mDNS: нет подходящих интерфейсов, fallback")
            return ServiceAnnouncer._fallback_ip()

        # приоритет: сначала с broadcast, потом остальные
        with_broadcast = [c for c in candidates if c[2]]
        pool = with_broadcast if with_broadcast else candidates

        # приоритет по диапазону IP
        def priority(item: tuple[str, str, bool]) -> int:
            ip = item[1]
            if ip.startswith("192.168."):
                return 0
            if ip.startswith("10."):
                return 1
            return 2

        pool.sort(key=priority)
        iface, ip, has_bcast = pool[0]

        log.info(">>> mDNS: выбран iface=%s ip=%s (broadcast=%s)",
                 iface, ip, has_bcast)
        log.info(">>> mDNS: всего кандидатов: %s",
                 [(c[0], c[1]) for c in candidates])

        return ip

    @staticmethod
    def _fallback_ip() -> str | None:
        """
        Старый способ — connect(8.8.8.8).
        Может выбрать VPN, если он активен.
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