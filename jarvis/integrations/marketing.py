"""Online presence data: social followers, Google reviews, Search Console rankings, PageSpeed."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from ..config import Settings

log = logging.getLogger(__name__)


class PresenceSources:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self._google_token: tuple[str, float] | None = None

    def configured(self) -> dict[str, bool]:
        s = self.s
        return {
            "facebook": bool(s.meta_page_id and s.meta_page_token),
            "instagram": bool(s.instagram_business_id and s.meta_page_token),
            "linkedin": bool(s.linkedin_org_id and s.linkedin_access_token),
            "tiktok": bool(s.tiktok_access_token),
            "google_reviews": bool(s.google_places_api_key and s.google_place_id),
            "search_console": bool(s.google_service_account_file and Path(s.google_service_account_file).exists()),
        }

    # -- socials --------------------------------------------------------------------
    async def facebook(self) -> dict[str, float]:
        r = await self.http.get(f"https://graph.facebook.com/{self.s.meta_graph_version}/{self.s.meta_page_id}",
                                params={"fields": "followers_count,fan_count", "access_token": self.s.meta_page_token})
        r.raise_for_status()
        d = r.json()
        return {"followers": d.get("followers_count") or d.get("fan_count") or 0}

    async def instagram(self) -> dict[str, float]:
        r = await self.http.get(f"https://graph.facebook.com/{self.s.meta_graph_version}/{self.s.instagram_business_id}",
                                params={"fields": "followers_count,media_count", "access_token": self.s.meta_page_token})
        r.raise_for_status()
        d = r.json()
        return {"followers": d.get("followers_count", 0), "posts": d.get("media_count", 0)}

    async def linkedin(self) -> dict[str, float]:
        urn = quote(f"urn:li:organization:{self.s.linkedin_org_id}", safe="")
        r = await self.http.get(f"https://api.linkedin.com/rest/networkSizes/{urn}",
                                params={"edgeType": "COMPANY_FOLLOWED_BY_MEMBER"},
                                headers={"Authorization": f"Bearer {self.s.linkedin_access_token}",
                                         "LinkedIn-Version": self.s.linkedin_version,
                                         "X-Restli-Protocol-Version": "2.0.0"})
        r.raise_for_status()
        return {"followers": r.json().get("firstDegreeSize", 0)}

    async def tiktok(self) -> dict[str, float]:
        r = await self.http.get("https://open.tiktokapis.com/v2/user/info/",
                                params={"fields": "follower_count,likes_count,video_count"},
                                headers={"Authorization": f"Bearer {self.s.tiktok_access_token}"})
        r.raise_for_status()
        u = r.json().get("data", {}).get("user", {})
        return {"followers": u.get("follower_count", 0), "likes": u.get("likes_count", 0),
                "videos": u.get("video_count", 0)}

    async def google_reviews(self) -> dict[str, float]:
        r = await self.http.get(f"https://places.googleapis.com/v1/places/{self.s.google_place_id}",
                                headers={"X-Goog-Api-Key": self.s.google_places_api_key,
                                         "X-Goog-FieldMask": "displayName,rating,userRatingCount"})
        r.raise_for_status()
        d = r.json()
        return {"rating": d.get("rating", 0), "reviews": d.get("userRatingCount", 0)}

    # -- Google Search Console ----------------------------------------------------------
    async def _google_access_token(self) -> str:
        if self._google_token and self._google_token[1] > time.time() + 60:
            return self._google_token[0]
        import jwt  # PyJWT, installed with msal

        key = json.loads(Path(self.s.google_service_account_file).read_text())
        now = int(time.time())
        assertion = jwt.encode({"iss": key["client_email"], "scope": "https://www.googleapis.com/auth/webmasters.readonly",
                                "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600},
                               key["private_key"], algorithm="RS256")
        r = await self.http.post("https://oauth2.googleapis.com/token",
                                 data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        r.raise_for_status()
        tok = r.json()
        self._google_token = (tok["access_token"], time.time() + int(tok.get("expires_in", 3600)))
        return tok["access_token"]

    async def search_performance(self, days: int = 28, dimension: str = "query", limit: int = 25) -> list[dict[str, Any]]:
        end = date.today() - timedelta(days=2)  # Search Console data lags ~2 days
        start = end - timedelta(days=days - 1)
        site = quote(self.s.search_console_site, safe="")
        r = await self.http.post(
            f"https://www.googleapis.com/webmasters/v3/sites/{site}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {await self._google_access_token()}"},
            json={"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": [dimension],
                  "rowLimit": limit})
        r.raise_for_status()
        return [{dimension: row["keys"][0], "clicks": row["clicks"], "impressions": row["impressions"],
                 "ctr_pct": round(100 * row["ctr"], 2), "avg_position": round(row["position"], 1)}
                for row in r.json().get("rows", [])]

    async def pagespeed(self, url: str, strategy: str = "mobile") -> dict[str, Any]:
        params: list[tuple[str, str]] = [("url", url), ("strategy", strategy), ("category", "PERFORMANCE"),
                                         ("category", "SEO"), ("category", "ACCESSIBILITY"),
                                         ("category", "BEST_PRACTICES")]
        if self.s.pagespeed_api_key:
            params.append(("key", self.s.pagespeed_api_key))
        r = await self.http.get("https://www.googleapis.com/pagespeedonline/v5/runPagespeed", params=params, timeout=120)
        r.raise_for_status()
        lh = r.json().get("lighthouseResult", {})
        cats = {k: round(100 * (v.get("score") or 0)) for k, v in lh.get("categories", {}).items()}
        audits = lh.get("audits", {})
        failing = [a.get("title") for a in audits.values()
                   if a.get("score") is not None and a.get("score") < 0.9 and a.get("scoreDisplayMode") == "binary"]
        return {"strategy": strategy, "scores": cats,
                "largest_contentful_paint": audits.get("largest-contentful-paint", {}).get("displayValue"),
                "failing_checks": failing[:15]}
