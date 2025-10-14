# app/sub/zarinpal.py (بروزرسانی برای لاگ)
import httpx
import os
import logging

LOG = logging.getLogger("payment.zarinpal")

MERCHANT_ID = os.getenv("ZARINPAL_MERCHANT_ID", "d7537698-4d9e-4a23-9457-ae1dbd7d10a2")
CALLBACK_URL = os.getenv("ZARINPAL_CALLBACK_URL", "http://127.0.0.1:8000/payment/callback")
SANDBOX = os.getenv("ZARINPAL_SANDBOX", "false").lower() == "true"

if SANDBOX:
    ZP_API_REQUEST = "https://sandbox.zarinpal.com/pg/v4/payment/request.json"
    ZP_API_VERIFY = "https://sandbox.zarinpal.com/pg/v4/payment/verify.json"
    ZP_API_STARTPAY = "https://sandbox.zarinpal.com/pg/StartPay/{authority}"
else:
    ZP_API_REQUEST = "https://api.zarinpal.com/pg/v4/payment/request.json"
    ZP_API_VERIFY = "https://api.zarinpal.com/pg/v4/payment/verify.json"
    ZP_API_STARTPAY = "https://www.zarinpal.com/pg/StartPay/{authority}"

async def create_payment(amount: int, description: str, chat_id: str) -> dict:
    payload = {
        "merchant_id": MERCHANT_ID,
        "amount": amount,
        "description": description,
        "callback_url": f"{CALLBACK_URL}?chat_id={chat_id}",
    }
    LOG.info("Zarinpal.create_payment request payload: %s", payload)
    print(">>> MERCHANT_ID repr:", repr(MERCHANT_ID))

    try:
        async with httpx.AsyncClient() as client:
            print(">>> Using request URL:", ZP_API_REQUEST)

            resp = await client.post(ZP_API_REQUEST, json=payload, timeout=15)
        LOG.info("Zarinpal.create_payment response status=%s body=%s", resp.status_code, resp.text)
        data = resp.json()
    except Exception:
        LOG.exception("HTTP error when requesting Zarinpal")
        return {"ok": False, "error": "network_error"}

    if data.get("data") and data["data"].get("authority"):
        authority = data["data"]["authority"]
        return {"ok": True, "authority": authority, "url": ZP_API_STARTPAY.format(authority=authority)}
    LOG.warning("Zarinpal.create_payment returned error: %s", data.get("errors"))
    return {"ok": False, "error": data.get("errors")}

async def verify_payment(amount: int, authority: str) -> dict:
    payload = {"merchant_id": MERCHANT_ID, "amount": amount, "authority": authority}
    LOG.info("Zarinpal.verify_payment payload=%s", payload)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(ZP_API_VERIFY, json=payload, timeout=15)
        LOG.info("Zarinpal.verify_payment status=%s body=%s", resp.status_code, resp.text)
        data = resp.json()
    except Exception:
        LOG.exception("HTTP error when verifying Zarinpal")
        return {"ok": False, "error": "network_error"}

    if data.get("data") and data["data"].get("code") == 100:
        return {"ok": True, "ref_id": data["data"]["ref_id"]}
    LOG.warning("Zarinpal.verify_payment returned error: %s", data.get("errors"))
    return {"ok": False, "error": data.get("errors")}
