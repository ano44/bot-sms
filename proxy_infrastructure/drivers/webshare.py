"""
webshare.py
===========
برنامج تشغيل (Driver) لمزود البروكسيات Webshare.
يدعم تدوير الـ IP عبر الـ API الخاص بـ Webshare عند الحاجة.
"""

import httpx
import socks
import asyncio
from .base import BaseProxyDriver

class WebshareProxyDriver(BaseProxyDriver):
    
    async def get_proxy(self, country_code: str) -> dict | None:
        """
        لأن Webshare يعتمد على منافذ ثابتة نقوم بإضافتها لقاعدة البيانات،
        فإن منطق جلب المنفذ هو نفس البروكسي الثابت.
        """
        import database as db
        def _get():
            # نبحث عن البروكسي المضاف تحت اسم المزود WEBSHARE
            row = db.db_execute("""
                SELECT id, proxy_type, host, port, username, password, provider, rotation_url
                FROM proxies
                WHERE country_code = %s AND is_active = TRUE AND provider = 'WEBSHARE'
                ORDER BY (success_count / COALESCE(NULLIF(success_count + failure_count, 0), 1.0)) DESC
                LIMIT 1
            """, (country_code.upper(),), commit=False, fetch='one')
            if row:
                return {
                    "id": row[0],
                    "proxy_type": row[1],
                    "host": row[2],
                    "port": row[3],
                    "username": row[4],
                    "password": row[5],
                    "provider": row[6],
                    "rotation_url": row[7],
                    "country_code": country_code.upper()
                }
            return None
        
        return await asyncio.to_thread(_get)

    async def rotate_ip(self, rotation_url: str) -> bool:
        """
        طلب تدوير الـ IP لبروكسي Webshare.
        يحتاج إلى إرسال طلب HTTP GET إلى رابط التدوير (rotation_url).
        """
        if not rotation_url:
            return False
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(rotation_url)
                if res.status_code == 200:
                    # ننتظر قليلاً حتى يستقر الـ IP الجديد لدى السيرفر
                    await asyncio.sleep(2.0)
                    return True
        except Exception:
            pass
        return False

    async def check_health(self, proxy_config: dict) -> bool:
        """فحص سلامة منفذ البروكسي."""
        host = proxy_config.get("host")
        port = int(proxy_config.get("port"))
        username = proxy_config.get("username")
        password = proxy_config.get("password")
        ptype = socks.SOCKS5
        
        try:
            def _check():
                s = socks.socksocket()
                s.set_proxy(ptype, host, port, True, username, password)
                s.settimeout(5.0)
                try:
                    s.connect(("149.154.167.50", 443))
                    return True
                except Exception:
                    return False
                finally:
                    s.close()
            
            return await asyncio.to_thread(_check)
        except Exception:
            return False
