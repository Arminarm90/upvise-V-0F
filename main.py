from app.bot import build_app
from app.config import settings
import logging

def main():
    logging.basicConfig(level=logging.INFO)  # ← لاگ را روشن کن
    app = build_app()
    print("🚀 starting polling...")
    app.run_polling()

if __name__ == "__main__":
    if not settings.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN در .env تنظیم نشده")
    main()
