"""
manager.py
==========
الوسيط الرئيسي الموحد لإدارة البروكسيات (Proxy Manager Subsystem).
يمثل الواجهة الوحيدة التي يتعامل معها النظام الخارجي لطلب البروكسي وتحديث الإحصائيات.
"""

import time
import socks
import logging
from .pool import proxy_pool
from .evaluator import proxy_evaluator
from .drivers import get_driver_instance

logger = logging.getLogger(__name__)

class ProxyManager:
    def __init__(self):
        # تتبع مؤقت للبروكسي المخصص لكل عملية فحص حالية
        # process_id -> proxy_data dict
        self._active_sessions = {}
        
        # تتبع وقت بدء الفحص لحساب البنج (Latency) بدقة
        # process_id -> start_time timestamp
        self._session_start_times = {}

    async def get_proxy_for_telegram(self, phone: str, session_id: str) -> tuple | None:
        """
        طلب بروكسي مناسب وجاهز للاستخدام لعملية فحص معينة.
        تُرجع tuple متوافق مع Telethon:
        (socks.SOCKS5, host, port, True, username, password)
        أو None إذا لم يتوفر بروكسي.
        """
        # جلب بروكسي مرشح ومناسب من الـ Pool
        proxy_data = await proxy_pool.acquire_proxy(phone)
        if not proxy_data:
            return None

        # تسجيل البروكسي وتوقيت البدء لهذه الجلسة
        self._active_sessions[session_id] = proxy_data
        self._session_start_times[session_id] = time.time()

        # بناء الـ tuple المتوافق مع مكتبة Telethon
        return self._build_telethon_proxy(proxy_data)

    async def release_proxy(self, session_id: str, is_success: bool, is_flood: bool = False):
        """
        تحرير البروكسي بعد انتهاء الفحص وتحديث إحصائيات الجودة والسرعة وقاطع الدائرة.
        """
        proxy_data = self._active_sessions.pop(session_id, None)
        start_time = self._session_start_times.pop(session_id, None)

        if not proxy_data:
            return

        proxy_id = proxy_data["id"]
        
        # تحرير الـ Load Balancer
        await proxy_pool.release_proxy(proxy_id)

        # حساب زمن الاستجابة (Latency)
        latency = 0.0
        if start_time:
            latency = time.time() - start_time

        # تحديث التقييم والـ Circuit Breaker
        if is_success:
            logger.info(f"[ProxyManager] Report Success for Proxy #{proxy_id} ({proxy_data['host']}:{proxy_data['port']}) | Latency: {latency:.2f}s")
            proxy_evaluator.report_success(proxy_id, latency=latency)
        else:
            logger.warning(f"[ProxyManager] Report Failure for Proxy #{proxy_id} ({proxy_data['host']}:{proxy_data['port']}) | Flood: {is_flood}")
            proxy_evaluator.report_failure(proxy_id, is_flood=is_flood)

    async def trigger_rotation(self, session_id: str) -> bool:
        """
        طلب تدوير الـ IP للبروكسي الحالي المرتبط بالجلسة في حال فشله.
        """
        proxy_data = self._active_sessions.get(session_id)
        if not proxy_data or not proxy_data.get("rotation_url"):
            return False

        provider = proxy_data.get("provider", "STATIC")
        rotation_url = proxy_data.get("rotation_url")
        
        logger.info(f"[ProxyManager] Triggering IP rotation for Proxy #{proxy_data['id']} via {provider} Driver...")
        
        try:
            driver = get_driver_instance(provider)
            success = await driver.rotate_ip(rotation_url)
            if success:
                logger.info(f"[ProxyManager] IP rotated successfully for Proxy #{proxy_data['id']}.")
                return True
        except Exception as e:
            logger.error(f"[ProxyManager] IP rotation failed for Proxy #{proxy_data['id']}: {e}")
            
        return False

    def _build_telethon_proxy(self, proxy_data: dict) -> tuple:
        """تحويل قاموس بيانات البروكسي لـ tuple متوافق مع Telethon."""
        proxy_type_map = {
            "SOCKS5": socks.SOCKS5,
            "SOCKS4": socks.SOCKS4,
            "HTTP": socks.HTTP,
        }
        ptype = proxy_type_map.get(proxy_data.get("proxy_type", "SOCKS5").upper(), socks.SOCKS5)
        host = proxy_data["host"]
        port = int(proxy_data["port"])
        username = proxy_data.get("username")
        password = proxy_data.get("password")

        if username and password:
            return (ptype, host, port, True, username, password)
        else:
            return (ptype, host, port)


proxy_manager = ProxyManager()
