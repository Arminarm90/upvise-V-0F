# app/sub/payment_service.py (بروزرسانی)
from app.sub.payments_db import PaymentsDB
from app.sub import zarinpal
import logging

LOG = logging.getLogger("payment.service")
payments = PaymentsDB()

async def start_payment(chat_id: str, amount: int, days: int, description: str = "خرید اشتراک"): # <--- اضافه شدن DAYS
    # 1. ذخیره رکورد پرداخت در DB (شامل تعداد روز)
    payment_id = payments.create_payment(chat_id, amount, "zarinpal", days)
    res = await zarinpal.create_payment(amount, description, chat_id)
    if not res["ok"]:
        payments.update_payment_status(payment_id, "failed")
        LOG.warning("create_payment failed: %s", res.get("error"))
        return {"ok": False, "error": res["error"]}

    authority = res["authority"]
    # persist authority in DB so callback (web process) میتونه پیدا کنه
    payments.update_payment_authority(payment_id, authority)
    LOG.info("created payment id=%s authority=%s", payment_id, authority)
    return {"ok": True, "payment_id": payment_id, "url": res["url"], "authority": authority}

async def confirm_payment(authority: str, amount: int):
    # lookup payment_id from DB (not in-memory map)
    payment_info = payments.get_payment_by_authority(authority)
    if not payment_info:
        LOG.error("confirm_payment: no payment_info for authority=%s", authority)
        return {"ok": False, "error": "payment_id not found"}

    payment_id = payment_info["id"]
    res = await zarinpal.verify_payment(amount, authority)
    if res.get("ok"):
        payments.update_payment_status(payment_id, "success", res["ref_id"])
        
        days_to_add = payment_info.get("days", 30) 
        
        payments.activate_subscription(
            chat_id=payment_info["chat_id"],
            days=days_to_add,  
            payment_id=payment_id,
        )
        # ----------------------------------------------------------------------
        
        LOG.info("payment verified id=%s ref=%s", payment_id, res["ref_id"])
        return {"ok": True, "ref_id": res["ref_id"]}
    else:
        payments.update_payment_status(payment_id, "failed")
        LOG.warning("payment verify failed id=%s error=%s", payment_id, res.get("error"))
        return {"ok": False, "error": res.get("error")}
