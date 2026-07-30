from telethon import TelegramClient
from telethon.sessions import StringSession
import asyncio
from collections import defaultdict


class SessionUnauthorizedError(Exception):
    """Raised when the Telegram session is not authorized."""
    pass


class TelegramClientManager:
    def __init__(self):
        self.clients = {}
        # قفل مستقل لكل account_id يمنع إنشاء عدة اتصالات Telethon متزامنة لنفس الحساب
        # (Race Condition عند أول استخدام لحساب لم يُخزَّن بعد في self.clients)
        self._locks = defaultdict(asyncio.Lock)

    async def get_client(self, account, proxy=None):
        """
        account = {
            "id": 1,
            "api_id": 12345,
            "api_hash": "xxxxx",
            "session": "xxxxx"
        }
        proxy = tuple بصيغة Telethon:
            (socks.SOCKS5, host, port) أو
            (socks.SOCKS5, host, port, True, username, password)
            None = بدون بروكسي (الافتراضي)
        """

        account_id = account["id"]

        # عند وجود بروكسي → نُنشئ عميلاً مؤقتاً خاصاً بهذه العملية
        # (لا نخزّنه في الكاش لأن الكاش يحتفظ بعميل بدون بروكسي للحسابات)
        if proxy is not None:
            return await self._create_proxy_client(account, proxy)

        async with self._locks[account_id]:
            if account_id in self.clients:
                client = self.clients[account_id]

                try:
                    if not client.is_connected():
                        await client.connect()

                    if await client.is_user_authorized():
                        return client
                    else:
                        # غير مخول، نقوم بقطع اتصاله لعدم تسريب المقبس
                        try:
                            await client.disconnect()
                        except Exception:
                            pass
                        self.clients.pop(account_id, None)
                except Exception:
                    # أي استثناء آخر (فشل شبكة، إلخ)، نقوم بقطع الاتصال وإزالته من الكاش لإعادة المحاولة من جديد
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    self.clients.pop(account_id, None)

            # --- المحاكاة الاحترافية لتطبيق الهاتف ---
            # ملاحظة: يجب استخدام نفس api_id الذي تم إنشاء الجلسة به، لكننا نموه بصمة الجهاز
            client = TelegramClient(
                StringSession(account["session"]),
                int(account["api_id"]),
                account["api_hash"],
                device_model='SM-S918B', # Samsung Galaxy S23 Ultra real model
                system_version='SDK 34', # Android 14
                app_version='10.14.5',
                lang_code='en',
                system_lang_code='en-US'
            )

            await client.connect()

            if not await client.is_user_authorized():
                await client.disconnect()
                raise SessionUnauthorizedError(
                    "Telegram session is not authorized."
                )

            self.clients[account_id] = client
            return client

    async def _create_proxy_client(self, account, proxy):
        """
        إنشاء عميل Telethon مؤقت عبر بروكسي محدد.
        لا يُخزَّن في الكاش — يُستخدم لمرة واحدة ثم يُغلق من الخارج.
        """
        client = TelegramClient(
            StringSession(account["session"]),
            int(account["api_id"]),
            account["api_hash"],
            proxy=proxy,
            device_model='SM-S918B',
            system_version='SDK 34',
            app_version='10.14.5',
            lang_code='en',
            system_lang_code='en-US'
        )

        await client.connect()

        if not await client.is_user_authorized():
            await client.disconnect()
            raise SessionUnauthorizedError(
                "Telegram session is not authorized (proxy client)."
            )

        return client

    async def disconnect_client(self, account_id):
        client = self.clients.pop(account_id, None)

        if client is None:
            return

        try:
            await client.disconnect()
        except Exception:
            pass

    async def disconnect_all(self):
        for client in list(self.clients.values()):
            try:
                await client.disconnect()
            except Exception:
                pass

        self.clients.clear()


telegram_client_manager = TelegramClientManager()

