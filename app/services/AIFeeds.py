import google.generativeai as genai
import asyncio
import logging
import os
from typing import List

LOG = logging.getLogger("AIFeeds")


class AIFeedsService:
    def __init__(self):
        key = os.getenv("AI_FEED_GEMINI_KEY")
        if not key:
            LOG.error("❌ AI_FEED_GEMINI_KEY missing")

        genai.configure(api_key=key)

        self.model = genai.GenerativeModel(
            "gemini-2.5-flash",
            generation_config={
                "temperature": 0.3,
                "max_output_tokens": 3000,
            }
        )

    async def generate_list(self, topic: str, lang: str = "en", max_results: int = 4) -> List[str]:

        # زبان از کلیدواژه کاربر است
        if lang == "fa":
            lang_instruction = (
                "The RSS feeds MUST belong to Iranian / Persian sources, news agencies, "
                "Persian tech websites, Persian blogs, or Persian media.\n"
                "Examples of allowed domains: iribnews, isna, irna, mehrnews, tasnimnews, khabaronline, zoomit, digiato.\n"
            )
        else:
            lang_instruction = (
                "The RSS feeds MUST belong to English international news or tech sources.\n"
                "Examples: Reuters, BBC, CNN, TechCrunch, Wired, Verge, APNews.\n"
            )

        prompt = (
            f"You are an RSS feed expert.\n"
            f"User keyword: '{topic}'\n"
            f"User language: {lang}\n\n"
            f"{lang_instruction}"
            "Return ONLY real RSS/Atom URLs.\n"
            "- Do NOT invent links.\n"
            "- Only return feeds you are 100% sure exist.\n"
            "- MUST be RSS/Atom, not homepages.\n"
            "- One URL per line.\n"
            "- No explanation.\n"
        )

        try:
            resp = await asyncio.to_thread(self.model.generate_content, prompt)

            if not resp or not resp.candidates:
                return []

            cand = resp.candidates[0]
            if cand.finish_reason != 1:
                return []

            out = getattr(resp, "text", "") or ""

            urls = [
                line.strip()
                for line in out.split("\n")
                if line.strip().startswith(("http://", "https://"))
            ]

            return urls[:max_results]

        except Exception:
            return []

