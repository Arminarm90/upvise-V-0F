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
                "max_output_tokens": 2000,
            }
        )

    async def generate_list(self, topic: str, max_results: int = 4) -> List[str]:

        prompt = (
            "You are a web assistant. Provide a SHORT list of well-known, high-traffic **news and technical websites** "
            f"related to this topic: '{topic}'. "
            "The links must be only the main RSS feeds. "
            "Return ONLY RSS feeds, one URL per line. No explanation, introduction, or formatting."
            "Important: Fine rss links eccording to user's lang. If the lang is persian, return links from persian sources. IN General pay attention to the user's lang"
        )

        try:
            # --- Call blocking SDK in thread ---
            resp = await asyncio.to_thread(self.model.generate_content, prompt)

            # ---- SAFETY & EMPTY CHECK ----
            if not resp or not resp.candidates:
                LOG.error("❌ No candidates returned")
                return []

            cand = resp.candidates[0]

            # If model blocked / safety
            if cand.finish_reason != 1:   # 1 == SUCCESS
                LOG.warning(f"⚠️ Gemini finish_reason = {cand.finish_reason}")
                return []

            # ---- Extract TEXT safely ----
            out = getattr(resp, "text", "") or ""

            urls = [
                line.strip()
                for line in out.split("\n")
                if line.strip().startswith(("http://", "https://"))
            ]

            return urls[:max_results]

        except Exception as e:
            LOG.error(f"Gemini error: {e}")
            return []
