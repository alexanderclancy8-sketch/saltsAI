"""Draft social-media graphics.

Default ("claude", needs no extra account or key): Jarvis's own Claude designs the advert as HTML + CSS + inline SVG
(services/adverts.py), which the console shows in a sandboxed frame and exports to PNG in the browser. These are designed
graphics - shapes, gradients, text and the company logo - not AI photographs.

Optional, only when an image API key was already set: the "openai" provider paints a plain PICTURE background, then Jarvis
itself lays the headline, Salts navy blue branding and the company logo on top (so the wording is always exact, never
garbled by the model) and saves a PNG. Kept for anyone who already uses it; nothing depends on it.

Safety rules, all enforced here rather than left to the model:
  * DRAFT ONLY - the design is saved and shown on the display for review. Nothing here posts, emails or uploads it.
  * An unsupported provider name (with a key set) is explained, never faked or substituted.
  * The OpenAI provider only ever receives a fixed, safe prompt built from an optional visual description - never the
    headline, customer names or anything else - and that prompt always forbids people, faces, text and logos.
  * Headline / sub-text / visual that contain emails, phone numbers or postcodes (customer or site details), or that
    ask for people or faces, are refused.
  * The API key is only ever sent in the provider request's Authorization header. It is never logged or returned.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from ..redact import describe_http_error
from .documents import LOGO_PATH, NAVY_HEX, TEAL_HEX

log = logging.getLogger(__name__)

# Pixel sizes of a standard feed post on each platform: (width, height).
PLATFORM_SIZES: dict[str, tuple[int, int]] = {
    "facebook": (1200, 630),
    "instagram": (1080, 1080),
    "linkedin": (1200, 627),
    "tiktok": (1080, 1920),
}
PLATFORM_LABELS = {"facebook": "Facebook", "instagram": "Instagram", "linkedin": "LinkedIn", "tiktok": "TikTok"}
MAX_HEADLINE = 90
MAX_SUBTEXT = 140
MAX_VISUAL = 300
MAX_LOGO_BYTES = 2_000_000
MAX_LOGO_SIDE = 4000
IMAGE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

OPENAI_URL = "https://api.openai.com/v1/images/generations"
DEFAULT_PROVIDER = "claude"
SUPPORTED_PROVIDERS = (DEFAULT_PROVIDER, "openai")

_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_POSTCODE = re.compile(r"\b[A-Za-z]{1,2}\d[A-Za-z\d]?\s*\d[A-Za-z]{2}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?44|0)[\s\-()]*\d(?:[\s\-()]*\d){8,10}(?!\d)")
_PEOPLE = re.compile(
    r"\b(face|faces|facial|portrait|selfie|headshot|celebrit\w*|lookalike|look-alike|person|people|man|men|woman|"
    r"women|child|children|kid|kids|boy|girl|worker|workers|engineer|engineers|staff|customer|customers|crowd|"
    r"team|owner|director)\b", re.I)


class ImageProviderError(Exception):
    """The provider answered, but not with a usable image."""


def _hex_rgb(value: str) -> tuple[int, int, int]:
    v = value.lstrip("#")
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


NAVY, TEAL = _hex_rgb(NAVY_HEX), _hex_rgb(TEAL_HEX)


# ------------------------------------------------------------------------------------------------ providers
def _provider_size(size: tuple[int, int]) -> str:
    w, h = size
    return "1536x1024" if w > h else "1024x1536" if h > w else "1024x1024"


async def _openai(http, key: str, model: str, prompt: str, size: tuple[int, int]) -> bytes:
    r = await http.post(OPENAI_URL, headers={"Authorization": f"Bearer {key}"}, timeout=120,
                        json={"model": model, "prompt": prompt, "size": _provider_size(size), "n": 1})
    r.raise_for_status()
    items = r.json().get("data") or []
    b64 = items[0].get("b64_json") if items and isinstance(items[0], dict) else None
    if not b64:
        raise ImageProviderError("the provider returned no image")
    return base64.b64decode(b64)


# provider name (the IMAGE_PROVIDER setting) -> async function(http, key, model, prompt, size) -> image bytes
PROVIDERS = {"openai": _openai}


# ------------------------------------------------------------------------------------------------ checks
def check_text(headline: str, subtext: str, visual: str) -> str:
    """'' if the wording is fine, otherwise a plain-English reason it is refused."""
    for label, text in (("headline", headline), ("sub-text", subtext), ("visual description", visual)):
        if _EMAIL.search(text) or _POSTCODE.search(text) or _PHONE.search(text):
            return (f"The {label} contains what looks like an email address, phone number or postcode. Graphics must "
                    "not carry customer or site details - take it out (add contact details yourself when posting).")
    hit = _PEOPLE.search(visual)
    if hit:
        return (f"I won't render people or faces ('{hit.group(0)}' in the visual description). Describe objects or "
                "abstract shapes instead, e.g. a fire alarm panel, a smoke detector, a padlock, a shield.")
    return ""


def build_prompt(visual: str) -> str:
    subject = visual.strip() or ("subtle fire safety and security themes: a shield, a padlock, a flame silhouette "
                                 "and a smoke detector outline")
    return (f"A clean, modern, professional background image for a company social media post. Subject: {subject}. "
            "Deep navy blue colour palette with lighter blue and teal accents, soft lighting, and calm empty space "
            "for overlaid text. Strictly NO people, NO faces, NO hands, NO text, NO letters, NO logos, NO watermarks "
            "and NO recognisable real buildings, street signs, vehicles or number plates.")


# ------------------------------------------------------------------------------------------------ logo
def images_dir(settings) -> Path:
    return Path(settings.data_dir) / "generated_images"


def uploaded_logo_path(settings) -> Path:
    return Path(settings.data_dir) / "brand" / "logo.png"


def logo_source(settings) -> tuple[str, Path | None]:
    """('uploaded' | 'bundled' | 'none', path). An uploaded logo wins over the one shipped with Jarvis."""
    up = uploaded_logo_path(settings)
    if up.exists():
        return "uploaded", up
    if LOGO_PATH.exists():
        return "bundled", LOGO_PATH
    return "none", None


def save_logo(settings, data: bytes) -> Path:
    """Validate an uploaded logo (PNG/JPEG/WebP, small) and keep it as the company logo. Raises ValueError."""
    from PIL import Image

    if not data:
        raise ValueError("The file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("The logo is too large (max 2 MB).")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()
        if fmt not in ("PNG", "JPEG", "WEBP"):
            raise ValueError("Use a PNG, JPEG or WebP logo.")
        with Image.open(io.BytesIO(data)) as im:
            if max(im.size) > MAX_LOGO_SIDE:
                raise ValueError(f"The logo is too big - keep it under {MAX_LOGO_SIDE} pixels on each side.")
            clean = im.convert("RGBA")
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001 - any decoder failure means "not a usable image"
        raise ValueError("That file isn't a readable image.") from e
    path = uploaded_logo_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    clean.save(tmp, format="PNG")
    tmp.replace(path)
    return path


# ------------------------------------------------------------------------------------------------ drawing
def _font(size: int):
    from PIL import ImageFont

    candidates: list[str] = []
    try:
        import reportlab

        candidates.append(str(Path(reportlab.__file__).parent / "fonts" / "VeraBd.ttf"))
    except ImportError:  # pragma: no cover - reportlab is a dependency
        pass
    candidates += ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "arialbd.ttf"]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # older Pillow: no scalable default
        return ImageFont.load_default()


def _wrap(text: str, font, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = ""
        while font.getlength(word) > max_width and len(word) > 1:  # one huge word: split it by letters
            cut = len(word) - 1
            while cut > 1 and font.getlength(word[:cut]) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    return lines or [""]


def compose(background: bytes, headline: str, subtext: str, size: tuple[int, int],
            logo_path: Path | None) -> tuple[bytes, bool]:
    """Navy-branded PNG: the provider's background tinted navy, a navy text panel with the headline, the logo on a
    white rounded panel top-left. Returns (png bytes, whether the logo was drawn)."""
    from PIL import Image, ImageDraw, ImageOps

    w, h = size
    short = min(w, h)
    bg = ImageOps.fit(Image.open(io.BytesIO(background)).convert("RGB"), (w, h))
    canvas = Image.blend(bg, Image.new("RGB", (w, h), NAVY), 0.5).convert("RGBA")

    margin = int(short * 0.07)
    max_text_w = w - 2 * margin
    head_px = int(short * 0.085)
    min_px = max(int(short * 0.045), 16)
    while True:
        head_font = _font(head_px)
        head_lines = _wrap(headline, head_font, max_text_w)
        if (len(head_lines) <= 4 and head_px * 1.2 * len(head_lines) <= h * 0.32) or head_px <= min_px:
            break
        head_px = max(int(head_px * 0.92), min_px)
    head_h = int(head_px * 1.2)
    sub_font = _font(max(int(head_px * 0.5), 14))
    sub_lines = _wrap(subtext, sub_font, max_text_w)[:3] if subtext else []
    sub_h = int(getattr(sub_font, "size", head_px * 0.5) * 1.3)

    pad = int(margin * 0.8)
    bar = max(6, h // 150)
    panel_h = bar + pad + head_h * len(head_lines) + (int(pad * 0.5) + sub_h * len(sub_lines) if sub_lines else 0) + pad
    panel_top = h - panel_h
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rectangle([0, panel_top, w, h], fill=NAVY + (240,))
    d.rectangle([0, panel_top, w, panel_top + bar], fill=TEAL + (255,))
    canvas = Image.alpha_composite(canvas, overlay)

    d = ImageDraw.Draw(canvas)
    y = panel_top + bar + pad
    for line in head_lines:
        d.text((margin, y), line, font=head_font, fill=(255, 255, 255, 255))
        y += head_h
    if sub_lines:
        y += int(pad * 0.5)
        for line in sub_lines:
            d.text((margin, y), line, font=sub_font, fill=(201, 214, 238, 255))
            y += sub_h

    logo_drawn = False
    if logo_path is not None:
        try:
            with Image.open(logo_path) as raw:
                logo = raw.convert("RGBA")
            scale = min(w * 0.28 / logo.width, h * 0.14 / logo.height)
            logo = logo.resize((max(1, int(logo.width * scale)), max(1, int(logo.height * scale))))
            inset = max(8, int(margin * 0.25))
            box = [margin, margin, margin + logo.width + 2 * inset, margin + logo.height + 2 * inset]
            d.rounded_rectangle(box, radius=inset, fill=(255, 255, 255, 255))
            canvas.paste(logo, (margin + inset, margin + inset), logo)
            logo_drawn = True
        except Exception as e:  # noqa: BLE001 - an unreadable logo must not stop the draft
            log.warning("logo not drawn on graphic: %s", type(e).__name__)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue(), logo_drawn


# ------------------------------------------------------------------------------------------------ service
class ImageGenerator:
    def __init__(self, j):
        self.j = j

    def provider(self) -> str:
        """Which maker is used: "claude" (the default, always available) or "openai" (only when an image key is set).
        An old "openai" choice with no key quietly means the Claude designer - nothing needs a key."""
        s = self.j.settings
        chosen = (s.image_provider or DEFAULT_PROVIDER).strip().lower()
        if chosen == "openai":
            return "openai" if s.image_api_key else DEFAULT_PROVIDER
        if chosen == DEFAULT_PROVIDER or not s.image_api_key:
            return DEFAULT_PROVIDER
        return chosen  # unsupported name with a key set: reported by not_connected()

    @property
    def configured(self) -> bool:
        return self.provider() in SUPPORTED_PROVIDERS

    def status(self) -> str:
        if not self.configured:
            return "not connected - the image provider isn't supported (choose Claude-designed graphics on Settings)"
        if self.provider() == "openai":
            return "connected (openai backgrounds) - drafts only, never posted"
        return ("ready - Claude-designed graphics (no extra account); designed shapes, text and logo, not AI photographs. "
                "Drafts only, never posted")

    def not_connected(self) -> dict[str, Any]:
        s = self.j.settings
        return {"connected": False,
                "error": (f"The image provider '{s.image_provider}' isn't supported. Set IMAGE_PROVIDER (or the Image "
                          f"generation provider on the Settings page) to one of: {', '.join(sorted(SUPPORTED_PROVIDERS))}.")}

    async def generate(self, headline: str, platform: str = "facebook", subtext: str = "",
                       visual: str = "") -> dict[str, Any]:
        """Make a draft graphic. Returns an {"error": ...} dict (never an image) when it can't be done honestly."""
        j = self.j
        if not self.configured:
            return self.not_connected()
        platform = (platform or "").strip().lower()
        if platform not in PLATFORM_SIZES:
            return {"error": f"Pick a platform: {', '.join(PLATFORM_SIZES)}."}
        headline = " ".join((headline or "").split())
        subtext = " ".join((subtext or "").split())
        visual = " ".join((visual or "").split())
        if not headline:
            return {"error": "I need a headline for the graphic."}
        if len(headline) > MAX_HEADLINE or len(subtext) > MAX_SUBTEXT or len(visual) > MAX_VISUAL:
            return {"error": f"Keep the headline under {MAX_HEADLINE} characters, the sub-text under {MAX_SUBTEXT} "
                             f"and the visual description under {MAX_VISUAL}."}
        refused = check_text(headline, subtext, visual)
        if refused:
            return {"error": refused}

        size = PLATFORM_SIZES[platform]
        if self.provider() == DEFAULT_PROVIDER:
            return await j.adverts.create(headline, subtext, visual, platform, size, PLATFORM_LABELS[platform])
        return await self._generate_openai(headline, platform, subtext, visual, size)

    async def _generate_openai(self, headline: str, platform: str, subtext: str, visual: str,
                               size: tuple[int, int]) -> dict[str, Any]:
        j, s = self.j, self.j.settings
        try:
            background = await PROVIDERS[s.image_provider.lower()](j.http, s.image_api_key, s.image_model,
                                                                   build_prompt(visual), size)
        except Exception as e:  # noqa: BLE001 - described without the request URL or any key
            reason = describe_http_error(e)
            log.warning("image provider call failed: %s", reason)
            return {"error": f"The image provider didn't give me an image ({reason}). Nothing was created."}
        source, logo_path = logo_source(s)
        try:
            png, logo_drawn = await asyncio.to_thread(compose, background, headline, subtext, size, logo_path)
        except Exception as e:  # noqa: BLE001 - e.g. the provider returned something that isn't an image
            log.warning("could not build the graphic: %s", type(e).__name__)
            return {"error": "The provider's picture couldn't be turned into a graphic. Nothing was created."}
        image_id = uuid.uuid4().hex
        try:
            folder = images_dir(s)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{image_id}.png").write_bytes(png)
        except OSError as e:
            log.warning("could not store the generated graphic: %s", type(e).__name__)
            return {"error": "I couldn't save the graphic just now, so there is nothing to show or download."}

        view_url = f"/api/images/{image_id}.png"
        note = ("Draft for review only - it has not been posted or sent anywhere. Check it before you use it.")
        logo_note = (f"Logo: {source} logo added." if logo_drawn else
                     "No company logo was added - upload one (POST /api/brand/logo) to have it on every graphic.")
        j.bus.publish("display", {
            "title": f"Draft {PLATFORM_LABELS[platform]} post graphic",
            "markdown": f"![Draft graphic]({view_url})\n\n**Draft for review - not posted anywhere.** "
                        f"{size[0]}x{size[1]} px for {PLATFORM_LABELS[platform]}. {logo_note}",
            "image_id": image_id})
        return {"shown_on_display": True, "image_id": image_id, "platform": platform,
                "size": f"{size[0]}x{size[1]}", "download_url": f"{view_url}?download=1",
                "logo": source if logo_drawn else "none", "note": f"{note} {logo_note}"}
