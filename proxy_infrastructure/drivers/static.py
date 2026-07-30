"""
static.py
=========
برنامج تشغيل (Driver) للبروكسيات الثابتة المخزنة في قاعدة البيانات.
"""

import socks
import socket
import asyncio
import database as db
from .base import BaseProxyDriver

class StaticProxyDriver(BaseProxyDriver):
    
    async def get_proxy(self, country_code: str) -> dict | None:
        """جلب بروكسي ثابت من قاعدة البيانات للبلد المختار."""
        # هذه الدالة ترجع البيانات مباشرة من قاعدة البيانات
        # ويتم استدعاء الدالة المخصصة في database.py
        def _get():
            return db.get_proxy_for_country(country_code)
        
        proxy_data = await asyncio.to_thread(_get)
        return proxy_data

    async def rotate_ip(self, rotation_url: str) -> bool:
        """البروكسيات الثابتة لا تدعم تدوير الـ IP التلقائي عبر API."""
        return False

    async def check_health(self, proxy_config: dict) -> bool:
        """فحص سلامة منفذ البروكسي مباشرة باستخدام socket SOCKS5."""
        host = proxy_config.get("host")
        port = int(proxy_config.get("port"))
        username = proxy_config.get("username")
        password = proxy_config.get("password")
        ptype_str = proxy_config.get("proxy_type", "SOCKS5").upper()
        if ptype_str == "HTTP":
            ptype = socks.HTTP
        elif ptype_str == "SOCKS4":
            ptype = socks.SOCKS4
        else:
            ptype = socks.SOCKS5
        
        try:
            # تشغيل الفحص في خيط منفصل لتجنب حظر الحلقة البرمجية
            def _check():
                s = socks.socksocket()
                s.set_proxy(ptype, host, port, True, username, password)
                s.settimeout(15.0)
                # نحاول الاتصال بخوادم تيليجرام مباشرة للتأكد
                try:
                    s.connect(("149.154.167.50", 443)) # خادم DC2 لتيليجرام
                    return True
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error(f"[STATIC_DRIVER] Connection failed for {host}:{port}. Error: {e}")
                    return False
                finally:
                    s.close()
            
            return await asyncio.to_thread(_check)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"[STATIC_DRIVER] Thread error: {e}")
            return False
