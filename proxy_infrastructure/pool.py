"""
pool.py
=======
إدارة مخزن البروكسيات وموازنة الأحمال (Proxy Pool & Load Balancer).
يوزع الطلبات بالتساوي بين البروكسيات النشطة ويتعامل مع التراجع الجغرافي.
"""

import logging
import asyncio
from collections import defaultdict
from .geo import get_fallback_countries
from .evaluator import proxy_evaluator
from .drivers import get_driver_instance

logger = logging.getLogger(__name__)

class ProxyPool:
    def __init__(self):
        # تتبع عدد الاتصالات النشطة حالياً لكل بروكسي (Load Balancing)
        # proxy_id -> active_connections_count
        self._active_connections = defaultdict(int)
        
        # الحد الأقصى للحسابات المتصلة بنفس البروكسي في نفس الوقت (2 حسابات لتجنب الحظر)
        self.MAX_CONNECTIONS_PER_IP = 2
        
        # قفل متزامن لحماية الـ Load Balancer من الـ Race Conditions
        self._lock = asyncio.Lock()

    async def acquire_proxy(self, phone: str) -> dict | None:
        """
        اختيار بروكسي مناسب للرقم المستهدف مع تطبيق معايير الجغرافيا وموازنة الأحمال وقاطع الدائرة.
        """
        from .geo import get_country_code_from_phone
        country_code = get_country_code_from_phone(phone)
        
        if not country_code:
            logger.warning(f"[ProxyPool] Could not detect country for phone: {phone}. Using global fallback.")
            return await self._get_global_fallback_proxy()

        async with self._lock:
            # 1. محاولة جلب البروكسي الأساسي للدولة المحددة
            proxy = await self._get_best_healthy_proxy_for_country(country_code)
            if proxy:
                self._active_connections[proxy["id"]] += 1
                logger.info(f"[ProxyPool] Match found: Proxy #{proxy['id']} ({country_code}) chosen for {phone}.")
                return proxy

            # 2. محاولة التراجع الجغرافي الإقليمي (Regional Fallback)
            fallback_countries = get_fallback_countries(country_code)
            logger.info(f"[ProxyPool] No active proxy for {country_code}. Trying regional fallbacks: {fallback_countries}")
            
            for fb_country in fallback_countries:
                proxy = await self._get_best_healthy_proxy_for_country(fb_country)
                if proxy:
                    self._active_connections[proxy["id"]] += 1
                    logger.info(f"[ProxyPool] Regional Fallback: Proxy #{proxy['id']} ({fb_country}) chosen for {phone} (original: {country_code}).")
                    return proxy

            # 3. التراجع العام (Global Fallback) لأعلى بروكسي نشط ومتاح
            logger.info(f"[ProxyPool] No regional fallback found. Reverting to global fallback.")
            proxy = await self._get_global_fallback_proxy()
            if proxy:
                self._active_connections[proxy["id"]] += 1
                logger.info(f"[ProxyPool] Global Fallback: Proxy #{proxy['id']} ({proxy['country_code']}) chosen for {phone}.")
                return proxy

        logger.warning(f"[ProxyPool] No proxy available anywhere in the pool for {phone}.")
        return None

    async def release_proxy(self, proxy_id: int):
        """تحرير البروكسي وتخفيض عدد الاتصالات النشطة عليه بعد انتهاء الفحص."""
        async with self._lock:
            if proxy_id in self._active_connections:
                self._active_connections[proxy_id] = max(0, self._active_connections[proxy_id] - 1)
                logger.debug(f"[ProxyPool] Released Proxy #{proxy_id}. Active connections count: {self._active_connections[proxy_id]}")

    async def _get_best_healthy_proxy_for_country(self, country_code: str) -> dict | None:
        """
        البحث عن أفضل بروكسي نشط، سليم، ويقع تحت حد الحمل المسموح به لدولة معينة.
        """
        import database as db
        
        # جلب البروكسي المرشح للدولة من قاعدة البيانات
        # يقوم كود SQL في database.py بترتيبهم تلقائياً حسب الجودة
        proxy = await asyncio.to_thread(db.get_proxy_for_country, country_code)
        if not proxy:
            return None

        # فحص قاطع الدائرة وحالة الحمل
        proxy_id = proxy["id"]
        if proxy_evaluator.is_healthy(proxy_id) and self._active_connections[proxy_id] < self.MAX_CONNECTIONS_PER_IP:
            return proxy
            
        return None

    async def _get_global_fallback_proxy(self) -> dict | None:
        """
        البحث عن أفضل بروكسي نشط متاح في النظام بأكمله متجاوزاً الجغرافيا.
        """
        import database as db
        
        all_proxies = await asyncio.to_thread(db.get_all_proxies)
        if not all_proxies:
            return None

        # تصفية البروكسيات النشطة والسليمة وتحت حد الحمل
        candidates = []
        for p in all_proxies:
            if p["is_active"] and proxy_evaluator.is_healthy(p["id"]) and self._active_connections[p["id"]] < self.MAX_CONNECTIONS_PER_IP:
                candidates.append(p)

        if not candidates:
            return None

        # ترتيب المرشحين حسب تقييم الجودة (Score) واختيار الأعلى
        candidates.sort(key=lambda x: proxy_evaluator.get_proxy_score(x), reverse=True)
        best_candidate = candidates[0]

        # تحويل الهيكل للقاموس المتوافق
        return {
            "id": best_candidate["id"],
            "proxy_type": best_candidate["proxy_type"],
            "host": best_candidate["host"],
            "port": best_candidate["port"],
            "username": best_candidate["username"],
            "password": best_candidate["password"],
            "provider": best_candidate["provider"],
            "rotation_url": best_candidate.get("rotation_url"),
            "country_code": best_candidate["country_code"]
        }


proxy_pool = ProxyPool()
