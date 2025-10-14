# app/web/payment_routes.py
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates
import logging
from telegram import Bot
from app.config import settings
from app.sub.payments_db import PaymentsDB
from app.sub.payment_service import confirm_payment
from app.utils.i18n import t, get_chat_lang

logger = logging.getLogger("payment.routes")
db = PaymentsDB()
router = APIRouter()

templates = Jinja2Templates(directory="templates")

@router.get("/payment/callback")
async def payment_callback(request: Request):
    try:
        chat_id = request.query_params.get("chat_id")
        authority = request.query_params.get("Authority")
        status = request.query_params.get("Status")
        lang = get_chat_lang(db, chat_id) if chat_id else "fa"

        bot = Bot(token=settings.telegram_token)

        # ❌ Authority وجود ندارد
        if not authority:
            return templates.TemplateResponse(
                "payment_failed.html",
                {"request": request, "error_message": t("payment.callback_error_generic", lang)}
            )

        payment_info = db.get_payment_by_authority(authority)

        # ❌ کاربر پرداخت را لغو کرده
        if status != "OK":
            if chat_id:
                await bot.send_message(chat_id, t("payment.callback_error_user_cancelled", lang))
            return templates.TemplateResponse(
                "payment_failed.html",
                {"request": request, "error_message": t("payment.callback_error_user_cancelled", lang)}
            )

        # ❌ رکورد پرداخت پیدا نشد
        if not payment_info:
            return templates.TemplateResponse(
                "payment_failed.html",
                {"request": request, "error_message": t("payment.callback_error_not_found", lang)}
            )

        # ✅ بررسی پرداخت
        amount = payment_info["amount"]
        result = await confirm_payment(authority, amount)

        if result.get("ok"):
            ref_id = result.get("ref_id")
            if chat_id:
                await bot.send_message(
                    chat_id, 
                    t("payment.success_message", lang).format(ref_id=ref_id)
                )
            return templates.TemplateResponse(
                "payment_success.html",
                {"request": request, "ref_id": ref_id}
            )
        else:
            error_msg = t("payment.callback_error_verify", lang)
            if chat_id:
                await bot.send_message(chat_id, error_msg)
            return templates.TemplateResponse(
                "payment_failed.html",
                {"request": request, "error_message": error_msg}
            )

    except Exception as e:
        logger.exception("Unhandled exception in payment_callback")
        return templates.TemplateResponse(
            "payment_failed.html",
            {"request": request, "error_message": t("payment.callback_error_generic", "fa")}
        )
