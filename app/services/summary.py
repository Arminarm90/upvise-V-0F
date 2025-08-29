# app/services/summary.py
# -*- coding: utf-8 -*-
from __future__ import annotations
from typing import Tuple, List, Optional
import json, re

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


def _system_prompt(lang: str) -> str:
    if (lang or "").lower().startswith("fa"):
        return (
            "خروجی فقط فارسی و روان باشد. لحن تحلیلی و کاربردی.\n"
            "دقیقاً JSON زیر را برگردان و هیچ متن اضافه ننویس:\n"
            "{"
            "\"tldr\":\"۱–۳ جمله جمع‌بندی تحلیلی، بدون تکرار عنوان\","
            "\"bullets\":[\"۳ تا ۶ نکتهٔ نتیجه‌محور؛ هر نکته با فعل شروع شود\"]"
            "}\n"
            "اگر داده ناکافی بود، محتاطانه خلاصه کن؛ اما فقط همین JSON."
        )
    return (
        "Output must be in clear English. Analytical and practical tone.\n"
        "Return exactly this JSON and nothing else:\n"
        "{"
        "\"tldr\":\"1–3 analytical sentences, do not repeat the title\","
        "\"bullets\":[\"3 to 6 action-oriented key points; each starts with a verb\"]"
        "}\n"
        "If content is limited, summarize cautiously; still return this JSON."
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
    return re.sub(r"^```(?:json)?|```$", "", (s or "").strip(), flags=re.I | re.M).strip()


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

def _force_lang(tldr: str, bullets: List[str], target_lang: str) -> Tuple[str, List[str]]:
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
    bullets_t = [ _translate(b, tgt) for b in (bullets or []) ]
    return tldr_t or tldr, bullets_t or bullets
# --------------------------------------------


class Summarizer:
    """
    Summary chain with Gemini primary and Lite fallback.
    Guarantees (tldr, bullets). Enforces user language if possible.
    """

    def __init__(self, api_key: Optional[str], prompt_lang: str = "fa"):
        self.api_key = api_key
        self.prompt_lang = (prompt_lang or "fa").lower()
        self._fail_count = 0
        self._cooldown_until: Optional[float] = None

    async def _call_ai(self, title: str, text: str) -> Tuple[str, List[str]]:
        if not (genai and self.api_key):
            return "", []

        import time
        if self._cooldown_until and time.time() < self._cooldown_until:
            return "", []

        try:
            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(settings.summary_model_name)

            prompt = (
                _system_prompt(self.prompt_lang)
                + f"\nTitle: {title or '-'}\n"
                + f"Content:\n{(text or '')[:settings.summary_max_input_chars]}"
            )

            resp = await model.generate_content_async(prompt)
            raw = _strip_code_fences(getattr(resp, "text", "") or "")
            data = json.loads(raw)

            tldr = (data.get("tldr") or "").strip()
            bullets = _dedupe_cap(
                [x for x in (data.get("bullets") or []) if isinstance(x, str)],
                cap=settings.summary_max_bullets,
            )

            # NEW: force output language if needed
            tldr, bullets = _force_lang(tldr, bullets, self.prompt_lang)

            self._fail_count = 0
            self._cooldown_until = None
            return tldr, bullets

        except Exception:
            self._fail_count += 1
            if self._fail_count >= settings.summary_cb_errors:
                import time
                self._cooldown_until = time.time() + settings.summary_cb_cooldown_sec
            return "", []

    # ---------- NEW: Lite summary with enforced language ----------
    # def _lite_summary(self, title: str, text: str) -> Tuple[str, List[str]]:
    #     """
    #     Heuristic TLDR + bullets from the raw text (no AI).
    #     Then enforces prompt_lang via translate helpers.
    #     """
    #     src = (text or "").strip()
    #     if not src:
    #         return "", []

    #     # TLDR: first 1–2 sentences (bounded)
    #     sentences = re.split(r"(?<=[.!؟\?])\s+", src)
    #     tldr = " ".join(sentences[:2]).strip()
    #     tldr = re.sub(r"\s+", " ", tldr)[:300]

    #     # bullets: pick ~3–6 meaningful lines
    #     points: List[str] = []
    #     for line in re.split(r"[\n\r]+", src):
    #         line = line.strip()
    #         if not line:
    #             continue
    #         if len(line) < 40:
    #             continue
    #         if any(k in line.lower() for k in ("should", "will", "can", "lead", "include", "increase", "reduce", "cause", "help", "need", "است", "می‌شود", "می‌تواند", "خواهد")):
    #             points.append(line)
    #         if len(points) >= getattr(settings, "summary_max_bullets", 4):
    #             break

    #     if not points:
    #         # fallback to longer sentences as bullets
    #         long_sents = [s for s in sentences if len(s) > 50]
    #         points = long_sents[: getattr(settings, "summary_max_bullets", 4)]

    #     bullets = _dedupe_cap(points, cap=getattr(settings, "summary_max_bullets", 4))

    #     # ENFORCE language
    #     tldr, bullets = _force_lang(tldr, bullets, self.prompt_lang)
    #     return tldr, bullets
    # -------------------------------------------------------------

    async def summarize(
        self, title: str, text: str, author: Optional[str] = None
    ) -> Tuple[str, List[str]]:
        title = (title or "").strip()
        text = (text or "").strip()

        base = (text if len(text) > settings.summary_lite_min_len else f"{title}\n{text}").strip()
        if not base:
            return "", []

        # Try AI and return the result directly
        tldr, bullets = await self._call_ai(title, base)
        
        # Optionally, you can add a second attempt here as well if the first one fails
        if not (tldr or bullets):
            tldr, bullets = await self._call_ai(title, base + "\n(Please ensure at least 3 bullet points.)")
            
        # Return AI result or empty if both attempts fail
        return tldr, bullets

        # 3) Lite fallback (guaranteed)
        # return self._lite_summary(title, base)
