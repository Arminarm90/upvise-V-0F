# app/services/summary.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Tuple, List, Optional
import json, re
import asyncio
import itertools

try:
    import google.generativeai as genai
except Exception:
    genai = None

# --- NEW: lightweight lang tools for Lite/AI output normalization ---
try:
    from langdetect import detect as _ld_detect
except Exception:
    _ld_detect = None

try:
    from deep_translator import GoogleTranslator as _GT
except Exception:
    _GT = None

from app.config import settings

# DEBUG
import logging

LOG = logging.getLogger("summary")


def _system_prompt(lang: str) -> str:
    if (lang or "").lower().startswith("fa"):
        return (
            "خروجی فقط فارسی و روان باشد. لحن تحلیلی و کاربردی.\n"
            "دقیقاً و فقط یک JSON با کلیدهای زیر برگردان؛ هیچ متن اضافه ننویس. "
            'اگر هر بخشی دادهٔ کافی نداشت، مقدار همان بخش را خالی بگذار (آرایهٔ خالی [] یا رشتهٔ خالی "").\n'
            "{"
            '"tldr":"۱–۳ جملهٔ جمع‌بندی تحلیلی؛ از تکرار عنوان خودداری کن",'
            '"bullets":["۳ تا ۶ نکتهٔ نتیجه‌محور؛ هر نکته با فعل شروع شود"],'
            '"opportunities":["فرصت‌های کلیدی، مختصر و عملی"],'
            '"risks":["ریسک‌ها/محدودیت‌ها، شفاف و واقع‌گرایانه"],'
            '"signal":"یک پیام/سیگنال کاربردی برای خواننده (در یک یا دو جمله)"'
            "}\n"
            "اگر داده ناکافی بود، محتاطانه خلاصه کن؛ اما باز هم فقط همین JSON را برگردان."
        )
    return (
        "Output must be in clear English with an analytical, practical tone.\n"
        "Return EXACTLY one JSON object with the keys below and nothing else. "
        'If any section lacks sufficient content, leave it empty (use [] for lists and "" for strings).\n'
        "{"
        '"tldr":"1–3 analytical sentences; do not repeat the title",'
        '"bullets":["3–6 action-oriented key points; each starts with a verb"],'
        '"opportunities":["Concise, actionable opportunities"],'
        '"risks":["Clear, realistic risks/limitations"],'
        '"signal":"One concise, practical takeaway for the reader"'
        "}\n"
        "If content is limited, summarize cautiously; still return ONLY this JSON."
    )


_CLEAN_BULLET_PREFIX = re.compile(r"^[•\-–—\*\u2022\+\s]+")


def _dedupe_cap(bullets: List[str], cap: int = 6) -> List[str]:
    seen, out = set(), []
    for b in bullets or []:
        b = _CLEAN_BULLET_PREFIX.sub("", (b or "").strip())
        b = re.sub(r"\s+", " ", b)
        if len(b) < 8:
            continue
        key = b.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
        if len(out) >= cap:
            break
    return out


def _strip_code_fences(s: str) -> str:
    return re.sub(
        r"^```(?:json)?|```$", "", (s or "").strip(), flags=re.I | re.M
    ).strip()


# ---------- NEW: translate helpers ----------
def _detect_lang(s: str) -> str:
    """fa/en/.. or '' if unknown"""
    if not s or not _ld_detect:
        return ""
    try:
        return (_ld_detect(s) or "").split("-", 1)[0].lower()
    except Exception:
        return ""


def _translate(s: str, target: str) -> str:
    """Translate using deep-translator; if unavailable, return s."""
    if not s:
        return s
    t = (target or "").lower()
    if not t or not _GT:
        return s
    try:
        return _GT(source="auto", target=t).translate(s)
    except Exception:
        return s


def _force_lang(
    tldr: str, bullets: List[str], target_lang: str
) -> Tuple[str, List[str]]:
    """
    If summary_strict is enabled or detected language != target, translate.
    Works for both AI and Lite outputs.
    """
    tgt = (target_lang or "").lower()
    if not tgt:
        return tldr, bullets

    strict = str(getattr(settings, "summary_strict", "false")).lower() == "true"

    # quick language guess from concatenated text
    sample = " ".join(([tldr] if tldr else []) + bullets)[:400]
    src = _detect_lang(sample)

    must_translate = strict or (src and src != tgt)
    if not must_translate:
        return tldr, bullets

    tldr_t = _translate(tldr, tgt) if tldr else ""
    bullets_t = [_translate(b, tgt) for b in (bullets or [])]
    return tldr_t or tldr, bullets_t or bullets


# ---------- NEW: language enforcement for premium fields ----------
def _force_lang_full(
    tldr: str,
    bullets: List[str],
    opportunities: List[str],
    risks: List[str],
    signal: str,
    target_lang: str,
) -> Tuple[str, List[str], List[str], List[str], str]:
    """
    همان منطق _force_lang اما روی همهٔ فیلدهای پرمیوم نیز اعمال می‌شود.
    """
    tldr2, bullets2 = _force_lang(tldr, bullets, target_lang)

    tgt = (target_lang or "").lower()
    if not tgt:
        return tldr2, bullets2, opportunities, risks, signal

    # تصمیم ترجمه بر اساس همان شرط سخت‌گیرانه
    strict = str(getattr(settings, "summary_strict", "false")).lower() == "true"
    sample = " ".join(
        (opportunities or []) + (risks or []) + ([signal] if signal else [])
    )[:400]
    src = _detect_lang(sample)
    must_translate = strict or (src and src != tgt)

    if not must_translate:
        return tldr2, bullets2, opportunities, risks, signal

    opp2 = [_translate(x, tgt) for x in (opportunities or [])]
    risk2 = [_translate(x, tgt) for x in (risks or [])]
    sig2 = _translate(signal, tgt) if signal else signal
    return tldr2, bullets2, opp2 or opportunities, risk2 or risks, sig2 or signal


# --------------------------------------------


def _extract_json(raw: str) -> str:
    """
    سعی می‌کنه از متن خام Gemini فقط JSON خالص رو بکشه بیرون.
    """
    if not raw:
        return "{}"
    # حذف بلاک‌های ```json ... ```
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.I | re.M).strip()
    # پیدا کردن اولین و آخرین { }
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned


# Get API Key
import itertools

# Round-Robin بجای random
_key_cycle = None


def get_gemini_key() -> str:
    global _key_cycle
    if not settings.gemini_keys:
        return ""
    if _key_cycle is None:
        _key_cycle = itertools.cycle(settings.gemini_keys)
    return next(_key_cycle)


# ---------- Lite summary (kept disabled) ----------
# def _lite_summary(title: str, text: str) -> Tuple[str, List[str]]:
#     """
#     Heuristic TLDR + bullets from the raw text (no AI).
#     Used as a robust fallback when AI fails or input is very short.
#     """
#     src = (text or "").strip()
#     if not src:
#         return "", []
#     # کوتاه‌سازی و جستجوی جملات معنی‌دار
#     sentences = re.split(r"(?<=[.!؟\?])\s+", src)
#     # tldr: دو جمله اول یا عنوان + جمله اول
#     tldr_candidates = [s for s in sentences if len(s) > 20]
#     tldr = ""
#     if tldr_candidates:
#         tldr = " ".join(tldr_candidates[:2]).strip()
#     else:
#         # اگر جمله بلند نداشت، بردار از متن کوتاه‌تر
#         tldr = (src[:200]).strip()

#     # bullets: خطوط/جملات که فعل دارند یا طولانی هستند
#     points: List[str] = []
#     for line in re.split(r"[\n\r]+", src):
#         l = line.strip()
#         if not l:
#             continue
#         if len(l) < 30:
#             continue
#         if any(
#             k in l.lower()
#             for k in (
#                 "should",
#                 "will",
#                 "can",
#                 "lead",
#                 "include",
#                 "increase",
#                 "reduce",
#                 "cause",
#                 "help",
#                 "need",
#                 "است",
#                 "می‌شود",
#                 "می‌تواند",
#                 "خواهد",
#             )
#         ):
#             points.append(l)
#         if len(points) >= getattr(settings, "summary_max_bullets", 4):
#             break

#     if not points:
#         # fallback: بردار جملات طولانی‌تر
#         long_sents = [s for s in sentences if len(s) > 40]
#         points = long_sents[: getattr(settings, "summary_max_bullets", 4)]

#     bullets = _dedupe_cap(points, cap=getattr(settings, "summary_max_bullets", 4))
#     # enforce language lightly
#     tldr, bullets = _force_lang(tldr, bullets, getattr(settings, "prompt_lang", "fa"))
#     return tldr, bullets


async def _call_ai(model, prompt: str) -> str:
    """یک فراخوانی امن به Gemini که raw text برمی‌گرداند یا ''."""
    try:
        resp = await model.generate_content_async(prompt)
        raw = _strip_code_fences(getattr(resp, "text", "") or "")
        return raw
    except Exception as ex:
        LOG.debug("_call_ai_once failed: %s", ex)
        return ""


class Summarizer:
    """
    Summary chain with Gemini primary and Lite fallback.
    Guarantees (tldr, bullets). Enforces user language if possible.
    """

    print("🔑 Gemini using key:", get_gemini_key())

    def __init__(self, api_key: Optional[str], prompt_lang: str = "fa"):
        self.api_key = api_key
        self.prompt_lang = (prompt_lang or "fa").lower()
        self._fail_count = 0
        self._cooldown_until: Optional[float] = None

    async def summarize_full(
        self, title: str, text: str, author: Optional[str] = None
    ) -> Tuple[str, List[str], List[str], List[str], str]:
        """
        تلاش چندمرحله‌ای حداکثری روی Gemini. اگر نتایج JSON نبودند
        تلاش می‌کنیم TLDR/bullets را از متن خام استخراج کنیم.
        """
        title = (title or "").strip()
        text = (text or "").strip()
        base = (
            text
            if len(text) > getattr(settings, "summary_lite_min_len", 120)
            else f"{title}\n{text}"
        ).strip()
        if not base:
            return "", [], [], [], ""

        if not (genai and self.api_key):
            LOG.debug("summarize_full: no genai or api key")
            return "", [], [], [], ""

        try:
            api_key = get_gemini_key()
            genai.configure(api_key=api_key)
            print("🔑 Gemini using key===", api_key)

            model = genai.GenerativeModel(settings.summary_model_name)
        except Exception as ex:
            LOG.exception("summarize_full: model init failed: %s", ex)
            return "", [], [], [], ""


        # تنظیمات قابل تغییر در config:
        max_attempts = int(getattr(settings, "summary_max_attempts", 6))
        short_threshold = int(getattr(settings, "summary_short_threshold", 120))
        max_input = int(getattr(settings, "summary_max_input_chars", 6000))

        # prompt های مرحله‌ای (از سخت به نرم)
        system = _system_prompt(self.prompt_lang)
        strict_json_prompt = (
            system + f"\nTitle: {title or '-'}\nContent:\n{(base or '')[:max_input]}"
        )
        softer_prompt = (
            strict_json_prompt
            + "\nIf you cannot output full JSON, try at least to return tldr and bullets in JSON."
        )
        unstructured_prompt = (
            "Provide a concise TLDR (1 sentence) and 2-4 short action bullets. "
            'If possible return JSON like {"tldr":"...","bullets":[...]}. Otherwise plain text is fine.\n'
            + f"Title: {title or '-'}\nContent:\n{(base or '')[:max_input]}"
        )
        just_tldr_prompt = (
            "Provide a single concise TLDR (one sentence) only.\n"
            + f"Title: {title}\nContent:\n{(base or '')[:max_input]}"
        )
        just_bullets_prompt = (
            "Provide 2-4 short action-oriented bullets only (one per line).\n"
            + f"Title: {title}\nContent:\n{(base or '')[:max_input]}"
        )

        prompt_sequence = [strict_json_prompt, softer_prompt, unstructured_prompt]
        if len(base) < short_threshold:
            prompt_sequence.append(just_tldr_prompt)
            prompt_sequence.append(just_bullets_prompt)
        prompt_sequence = prompt_sequence[:max_attempts]

        raw_collected = ""
        parsed_obj = {}

        def extract_from_raw(raw: str) -> dict:
            """سعی در استخراج JSON یا تکه‌های مفید از متن خام."""
            if not raw:
                return {}
            # 1) JSON extraction
            jtxt = _extract_json(raw)
            try:
                obj = json.loads(jtxt)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
            # 2) regex-based TLDR extraction
            # look for lines starting with TLDR, TL;DR, Summary:
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            out = {}
            for ln in lines[:5]:
                low = ln.lower()
                if (
                    low.startswith("tldr")
                    or low.startswith("tl;dr")
                    or low.startswith("summary")
                ):
                    # take after ":" if exists
                    if ":" in ln:
                        out["tldr"] = ln.split(":", 1)[1].strip()
                    else:
                        out["tldr"] = ln.strip()
                    break
            # bullets: lines that start with -, •, or numbered
            bullets = []
            for ln in lines:
                if ln.startswith(("-", "•", "*")) or re.match(r"^\d+[\).\s]", ln):
                    cleaned = re.sub(r"^[\-\•\*\d\.\)\s]+", "", ln).strip()
                    if len(cleaned) > 10:
                        bullets.append(cleaned)
            if bullets:
                out.setdefault("bullets", bullets)
            # fallback: if still empty, first sentence as tldr
            if not out.get("tldr"):
                first_sent = re.split(r"(?<=[.!؟\?])\s+", raw.strip())
                if first_sent:
                    out["tldr"] = first_sent[0][:300]
            return out

        # تلاش مرحله‌ای
        for idx, p in enumerate(prompt_sequence, 1):
            raw = await _call_ai(model, p)
            LOG.debug(
                "summarize_full attempt %d raw_len=%d title=%r",
                idx,
                len(raw or ""),
                title,
            )
            if not raw:
                continue
            raw_collected = raw
            parsed = extract_from_raw(raw)
            if (
                parsed.get("tldr")
                or parsed.get("bullets")
                or parsed.get("opportunities")
                or parsed.get("risks")
                or parsed.get("signal")
            ):
                parsed_obj = parsed
                LOG.debug(
                    "summarize_full: parsed from attempt %d -> keys=%s",
                    idx,
                    list(parsed.keys()),
                )
                break

        if not parsed_obj:
            raw = await _call_ai(model, just_tldr_prompt)
            parsed = extract_from_raw(raw)
            if parsed.get("tldr") or parsed.get("bullets"):
                parsed_obj = parsed
                LOG.debug("summarize_full: got fallback tldr/bullets")

        if not parsed_obj:
            if raw_collected:
                parsed_obj = extract_from_raw(raw_collected)
            else:
                return "", [], [], [], ""

        tldr = (parsed_obj.get("tldr") or "").strip()
        bullets = [x for x in (parsed_obj.get("bullets") or []) if isinstance(x, str)]
        opportunities = [
            x for x in (parsed_obj.get("opportunities") or []) if isinstance(x, str)
        ]
        risks = [x for x in (parsed_obj.get("risks") or []) if isinstance(x, str)]
        signal = (parsed_obj.get("signal") or "").strip()

        # caps و dedupe
        bullets = _dedupe_cap(bullets, cap=getattr(settings, "summary_max_bullets", 4))
        opp_cap = int(
            getattr(
                settings,
                "summary_max_opportunities",
                getattr(settings, "summary_max_bullets", 4),
            )
        )
        risk_cap = int(
            getattr(
                settings,
                "summary_max_risks",
                getattr(settings, "summary_max_bullets", 4),
            )
        )
        opportunities = _dedupe_cap(opportunities, cap=opp_cap)
        risks = _dedupe_cap(risks, cap=risk_cap)

        try:
            tldr, bullets, opportunities, risks, signal = _force_lang_full(
                tldr, bullets, opportunities, risks, signal, self.prompt_lang
            )
        except Exception:
            LOG.debug("summarize_full: _force_lang_full failed", exc_info=True)

        return tldr or "", bullets or [], opportunities or [], risks or [], signal or ""

    async def summarize(
        self, title: str, text: str, author: Optional[str] = None
    ) -> Tuple[str, List[str]]:
        title = (title or "").strip()
        text = (text or "").strip()

        base = (
            text if len(text) > settings.summary_lite_min_len else f"{title}\n{text}"
        ).strip()
        if not base:
            return "", []

        # 1) try AI chain (multiple attempts inside _call_ai)
        tldr, bullets = await self._call_ai(title, base)

        # 2) if AI returned nothing, try lite heuristic
        # if not (tldr or bullets):
        #     tldr, bullets = _lite_summary(title, base)

        # 3) final normalization/enforce language
        try:
            tldr, bullets = _force_lang(tldr, bullets, self.prompt_lang)
        except Exception:
            pass

        return tldr or "", bullets or []
