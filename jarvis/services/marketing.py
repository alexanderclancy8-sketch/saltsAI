"""Marketing & online presence: follower growth, Google reviews, search rankings and SEO audits."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from .. import demo_guard
from ..brain import llm

log = logging.getLogger(__name__)

LOCAL_TERMS = ["bradford", "leeds", "shipley", "baildon", "keighley", "halifax", "huddersfield", "wakefield",
               "harrogate", "york", "west yorkshire", "yorkshire"]
TRUST_TERMS = ["bafe", "nsi", "ssaib", "bs 5839", "bs5839", "bs 5266", "fia", "accredited", "certified"]
SERVICE_TERMS = ["fire alarm", "emergency lighting", "intruder alarm", "cctv", "access control", "fire extinguisher",
                 "maintenance", "servicing", "installation"]

MARKETING_SYSTEM = """You are Jarvis, marketing adviser to {company}, a fire & security installer/maintainer in West
Yorkshire. Write a short weekly marketing report for {owner}: follower growth per platform, Google reviews,
search visibility, then 3-5 specific, doable actions for this week that will grow enquiries and Google ranking
(local SEO, Google Business Profile, reviews, content ideas relevant to fire & security customers such as
schools, care homes, landlords, facilities managers). Plain British English, no fluff. Only use the data given;
say if something isn't connected."""


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.meta: dict[str, str] = {}
        self.h1: list[str] = []
        self.links: list[str] = []
        self.imgs_without_alt = 0
        self.imgs = 0
        self.jsonld: list[str] = []
        self.canonical = ""
        self._in: str | None = None
        self._buf: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("title", "h1") or (tag == "script" and a.get("type") == "application/ld+json"):
            self._in, self._buf = ("jsonld" if tag == "script" else tag), []
        elif tag == "meta" and (a.get("name") or a.get("property")):
            self.meta[(a.get("name") or a.get("property")).lower()] = a.get("content") or ""
        elif tag == "link" and "canonical" in (a.get("rel") or ""):
            self.canonical = a.get("href") or ""
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            self.imgs += 1
            self.imgs_without_alt += not (a.get("alt") or "").strip()

    def handle_endtag(self, tag):
        if self._in and (tag == self._in or (tag == "script" and self._in == "jsonld")):
            content = "".join(self._buf).strip()
            if self._in == "title":
                self.title = content
            elif self._in == "h1":
                self.h1.append(content)
            else:
                self.jsonld.append(content)
            self._in = None

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)
        self.text.append(data)


class MarketingTracker:
    PLATFORMS = ("facebook", "instagram", "linkedin", "tiktok", "google_reviews")

    def __init__(self, settings, db, http, sources, notifier, client):
        self.s = settings
        self.db = db
        self.http = http
        self.src = sources
        self.notifier = notifier
        self.client = client

    @property
    def connected(self) -> bool:
        return any(self.src.configured().values())

    @property
    def demo(self) -> bool:
        """Sample follower figures stand in (sample data on and nothing connected)."""
        return not self.connected and bool(getattr(self.s, "sample_data", True))

    async def snapshot(self) -> dict[str, Any]:
        """Record today's numbers for every connected platform (run daily)."""
        today = date.today().isoformat()
        results: dict[str, Any] = {}
        configured = self.src.configured()
        for platform in self.PLATFORMS:
            if not configured.get(platform):
                continue
            try:
                values = await getattr(self.src, platform)()
                for metric, value in values.items():
                    self.db.record_metric(today, platform, metric, float(value))
                results[platform] = values
            except Exception as e:  # noqa: BLE001
                log.warning("%s snapshot failed: %s", platform, e)
                results[platform] = {"error": str(e)[:200]}
        return results

    def _demo_series(self, platform: str, metric: str, days: int) -> list[dict[str, Any]]:
        start = {"facebook": 412, "instagram": 655, "linkedin": 289, "tiktok": 1180, "google_reviews": 37}[platform]
        rng = random.Random(platform)
        v, out = float(start), []
        for n in range(days, -1, -1):
            v += max(0, rng.gauss(0.6 if platform != "google_reviews" else 0.05, 1.0))
            out.append({"day": (date.today() - timedelta(days=n)).isoformat(),
                        "value": round(v) if metric != "rating" else 4.8})
        return out

    async def overview(self, days: int = 30) -> dict[str, Any]:
        since = (date.today() - timedelta(days=days)).isoformat()
        week_ago = (date.today() - timedelta(days=7)).isoformat()
        demo = self.demo
        if demo:
            demo_guard.touch(demo_guard.SOCIALS)  # sample follower counts: never handed to the model as real
        elif not self.connected:
            demo_guard.touch(demo_guard.SOCIALS, sample=False)  # nothing connected: "not connected", not "no followers"
        out: dict[str, Any] = {"demo": demo, "connected": self.src.configured(), "platforms": {}}
        if not demo and not self.connected:
            out["not_connected"] = demo_guard.panel_message(demo_guard.SOCIALS)
        for platform in self.PLATFORMS:
            metrics = ("rating", "reviews") if platform == "google_reviews" else ("followers",)
            pdata = {}
            for m in metrics:
                hist = self._demo_series(platform, m, days) if demo else self.db.metric_history(platform, m, since)
                if not hist:
                    continue
                latest = hist[-1]["value"]
                wk = next((h["value"] for h in hist if h["day"] >= week_ago), hist[0]["value"])
                pdata[m] = {"current": latest, "change_7d": round(latest - wk, 2),
                            "change_period": round(latest - hist[0]["value"], 2), "since": hist[0]["day"]}
            if pdata:
                out["platforms"][platform] = pdata
        return out

    async def search_rankings(self, days: int = 28) -> dict[str, Any]:
        if not self.src.configured()["search_console"]:
            return {"connected": False,
                    "note": "Google Search Console isn't connected - add a service account (see README). "
                            "Target keywords: " + self.s.seo_target_keywords}
        queries, pages = await asyncio.gather(self.src.search_performance(days, "query", 50),
                                              self.src.search_performance(days, "page", 15))
        targets = [k.strip().lower() for k in self.s.seo_target_keywords.split(",") if k.strip()]
        tracked = []
        for kw in targets:
            match = next((q for q in queries if kw in q["query"].lower()), None)
            tracked.append({"keyword": kw, **(match or {"avg_position": None, "note": "not in top queries"})})
        today = date.today().isoformat()
        for q in queries[:20]:
            self.db.record_metric(today, "search:" + q["query"][:80], "position", q["avg_position"])
        return {"connected": True, "period_days": days, "top_queries": queries[:25], "top_pages": pages,
                "target_keywords": tracked}

    async def seo_audit(self, url: str | None = None) -> dict[str, Any]:
        url = url or self.s.website_url
        findings: list[dict[str, str]] = []

        def add(level: str, issue: str, fix: str) -> None:
            findings.append({"level": level, "issue": issue, "fix": fix})

        r = await self.http.get(url, follow_redirects=True, timeout=30,
                                headers={"User-Agent": "Mozilla/5.0 (Jarvis SEO audit)"})
        page = _PageParser()
        page.feed(r.text)
        text = re.sub(r"\s+", " ", " ".join(page.text)).lower()
        final = str(r.url)
        if not final.startswith("https://"):
            add("high", "Site does not end up on HTTPS", "Force HTTPS redirects on every page.")
        if not page.title:
            add("high", "Missing <title>", "Add a title like 'Fire Alarm Installation & Servicing Bradford | Salts Fire and Security'.")
        elif not 30 <= len(page.title) <= 65:
            add("medium", f"Title is {len(page.title)} characters", "Aim for 50-60 characters with the main service + town.")
        desc = page.meta.get("description", "")
        if not desc:
            add("high", "No meta description", "Write a 140-160 character description with services, area and a call to action.")
        elif not 70 <= len(desc) <= 165:
            add("low", f"Meta description is {len(desc)} characters", "Aim for 140-160 characters.")
        if len(page.h1) != 1:
            add("medium", f"{len(page.h1)} H1 headings", "Use exactly one H1 describing the main service and area.")
        if not any("localbusiness" in j.lower() or "organization" in j.lower() for j in page.jsonld):
            add("high", "No LocalBusiness structured data", "Add schema.org LocalBusiness JSON-LD (name, address, phone, "
                                                            "opening hours, areaServed, sameAs social links).")
        if not page.canonical:
            add("low", "No canonical link", "Add <link rel='canonical'> to avoid duplicate-content issues.")
        if page.imgs and page.imgs_without_alt:
            add("medium", f"{page.imgs_without_alt}/{page.imgs} images have no alt text",
                "Describe each image (e.g. 'Gent Vigilon fire alarm panel installed at a Bradford school').")
        towns = [t for t in LOCAL_TERMS if t in text]
        if len(towns) < 3:
            add("high", f"Few local place names on the home page ({', '.join(towns) or 'none'})",
                "Mention the towns you cover and create a page per main area (Bradford, Leeds, Halifax...).")
        trust = [t for t in TRUST_TERMS if t in text]
        if not trust:
            add("medium", "No accreditations mentioned (BAFE, NSI, SSAIB, BS 5839...)",
                "Show accreditation logos and standards you work to - they are ranking and conversion signals.")
        services = [t for t in SERVICE_TERMS if t in text]
        missing_services = [t for t in SERVICE_TERMS[:6] if t not in text]
        if missing_services:
            add("medium", f"Services not mentioned on the home page: {', '.join(missing_services)}",
                "Give every service its own page and link to them from the home page.")
        words = len(text.split())
        if words < 400:
            add("medium", f"Thin home page (~{words} words)", "Aim for 600+ useful words: services, areas, sectors, FAQs.")
        base = f"{urlparse(final).scheme}://{urlparse(final).netloc}"
        for path, label in (("/robots.txt", "robots.txt"), ("/sitemap.xml", "sitemap.xml")):
            try:
                rr = await self.http.get(urljoin(base, path), timeout=15)
                if rr.status_code != 200:
                    add("medium", f"No {label}", f"Publish {label} and submit the sitemap in Google Search Console.")
            except Exception:  # noqa: BLE001
                add("low", f"Could not check {label}", "Check it exists.")
        speed = None
        try:
            speed = await self.src.pagespeed(final)
            if speed["scores"].get("performance", 100) < 70:
                add("high", f"Slow on mobile (performance {speed['scores']['performance']}/100)",
                    "Compress images (WebP), lazy-load below-the-fold images, remove unused scripts.")
        except Exception as e:  # noqa: BLE001
            speed = {"error": str(e)[:200]}
        order = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda f: order[f["level"]])
        return {"url": final, "title": page.title, "meta_description": desc, "h1": page.h1[:3],
                "local_terms_found": towns, "accreditations_found": trust, "services_found": services,
                "pagespeed": speed, "findings": findings,
                "always_do": ["Keep the Google Business Profile complete: categories, services, service areas, photos "
                              "weekly, and a post every week.",
                              "Ask every happy customer for a Google review - send the review link with the "
                              "service certificate.",
                              "Keep name, address and phone identical on the website, Google, Facebook, LinkedIn "
                              "and directories (Yell, Checkatrade, BAFE / NSI registers).",
                              "Publish a helpful article monthly (e.g. 'How often should a fire alarm be serviced? "
                              "BS 5839 explained') and share it on the socials."]}

    async def competitor_audit(self, competitors: list[str]) -> dict[str, Any]:
        """Us vs named local competitors: Google rating/reviews where a Places key is set, and the same SEO
        snapshot seo_audit() runs on our own site, run again against each competitor's website."""
        # Looking up OUR OWN listing (google_reviews()) needs both the API key and our place ID; looking up a
        # named competitor by text (find_business()) only ever needs the key - gating both on "google_reviews"
        # being fully configured meant a key-only setup (no place ID captured yet) silently skipped every
        # competitor lookup too, telling the owner to "add a Places API key" they'd already added.
        has_reviews = self.src.configured().get("google_reviews", False)
        has_places = bool(self.s.google_places_api_key)
        us: dict[str, Any] = {"name": self.s.company_name}
        if has_reviews:
            try:
                us.update(await self.src.google_reviews())
            except Exception as e:  # noqa: BLE001
                us["reviews_error"] = str(e)[:200]
        try:
            us["seo"] = await self.seo_audit()
        except Exception as e:  # noqa: BLE001
            us["seo_error"] = str(e)[:200]

        results = []
        for name in competitors[:6]:  # a handful at a time keeps this quick and the report readable
            entry: dict[str, Any] = {"query": name}
            if has_places:
                try:
                    entry.update(await self.src.find_business(name))
                except Exception as e:  # noqa: BLE001
                    entry["error"] = str(e)[:200]
            else:
                entry["note"] = "Add a Google Places API key on the Settings page to compare ratings/reviews."
            if entry.get("website"):
                try:
                    entry["seo"] = await self.seo_audit(entry["website"])
                except Exception as e:  # noqa: BLE001
                    entry["seo_error"] = str(e)[:200]
            results.append(entry)
        return {"us": us, "competitors": results}

    async def weekly_report(self) -> str:
        data = {"socials": await self.overview(30)}
        try:
            data["search"] = await self.search_rankings(28)
        except Exception as e:  # noqa: BLE001
            data["search"] = {"error": str(e)[:200]}
        text = await llm.write(self.client, self.s, system=MARKETING_SYSTEM.format(company=self.s.company_name,
                                                                                owner=self.s.owner_name),
                               prompt=json.dumps(data, default=str)[:40000], effort="medium")
        await self.notifier.notify("Weekly marketing report", text, level="info", push=True, importance="info")
        return text
