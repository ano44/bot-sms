"""
health_checker.py
=================
مهمة خلفية دورية لفحص كفاءة وسلامة جميع البروكسيات المضافة للنظام (Proxy Health Checker).
"""

import logging
import asyncio
import database as db
from ..drivers import get_driver_instance

logger = logging.getLogger(__name__)

async def check_all_proxies_health():
    """
    الدوران على جميع البروكسيات المتاحة في قاعدة البيانات،
    وفحص اتصالها، وتحديث حالتها لتفادي تمرير بروكسيات تالفة لـ Telethon.
    """
    logger.info("[HealthChecker] Starting proxy health check run...")
    
    # جلب جميع البروكسيات من قاعدة البيانات
    proxies = await asyncio.to_thread(db.get_all_proxies)
    if not proxies:
        logger.info("[HealthChecker] No proxies found in DB to check.")
        return

    for p in proxies:
        proxy_id = p["id"]
        provider = p.get("provider", "STATIC")
        host = p["host"]
        port = p["port"]
        
        # إعداد قاموس الإعدادات للفحص
        proxy_config = {
            "host": host,
            "port": port,
            "username": p.get("username"),
            "password": p.get("password"),
            "proxy_type": p.get("proxy_type", "SOCKS5")
        }

        # طباعة للتحقق من وجود الباسورد واليوزرنيم حقاً!
        u_len = len(str(proxy_config['username'])) if proxy_config['username'] else 0
        p_len = len(str(proxy_config['password'])) if proxy_config['password'] else 0
        logger.info(f"[HealthChecker] Checking #{proxy_id} ({host}:{port}) - UserLen: {u_len}, PassLen: {p_len}, Type: {proxy_config['proxy_type']}")

        try:
            # الحصول على نسخة من الـ Driver المناسب للمزود
            driver = get_driver_instance(provider)
            
            # تشغيل فحص السلامة للبروكسي
            is_alive = await driver.check_health(proxy_config)
            
            # تحديث حالة البروكسي في قاعدة البيانات
            # إذا كان معطلاً نوقفه، وإذا عاد للعمل نفعّله تلقائياً
            if is_alive:
                if not p["is_active"]:
                    logger.info(f"[HealthChecker] Proxy #{proxy_id} ({host}:{port}) is alive again. Enabling.")
                    await asyncio.to_thread(db.toggle_proxy, proxy_id, True)
            else:
                if p["is_active"]:
                    logger.warning(f"[HealthChecker] Proxy #{proxy_id} ({host}:{port}) is dead. Disabling.")
                    await asyncio.to_thread(db.toggle_proxy, proxy_id, False)
                    
        except Exception as e:
            logger.error(f"[HealthChecker] Error checking proxy #{proxy_id}: {e}")
            
    logger.info("[HealthChecker] Proxy health check run completed.")
