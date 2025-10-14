# app/web/server.py
from fastapi import FastAPI
import logging
from app.sub.payment_routes import router  # مسیرت را درست کن اگر فرق دارد

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("server")

app = FastAPI()
app.include_router(router)
