"""Draft social media graphics (generate_image): the OpenAI-background path that is kept for anyone who already has an image
key (the default, key-free Claude-designed HTML adverts are covered in tests/test_adverts.py), branding and sizes, safety
refusals, the logo, the owner-only download route, and that nothing is ever posted or logged with a key in it."""

import base64
import io
import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import jarvis
from jarvis.brain.tools import TOOLS_BY_NAME, ImageIn, generate_image
from jarvis.config import Settings
from jarvis.core import Jarvis
from jarvis.main import create_app
from jarvis.services import images
from jarvis.settings_store import FIELDS, SECTIONS
from tests.fakes import FakeClient

KEY = "sk-test-SECRETKEY1234567890"
IMAGE_ID = "a" * 32


def _png(color=(200, 30, 30), size=(64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _provider_ok(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png()).decode()}]})

    return handler


def _jarvis(tmp_path, key="", handler=None, **over) -> Jarvis:
    if key:  # these tests exercise the optional OpenAI path: a key was set and OpenAI chosen
        over.setdefault("image_provider", "openai")
    settings = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None, image_api_key=key, **over)
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return Jarvis(settings, http=http, client=FakeClient())


def _events(q) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _stored(j) -> list[Path]:
    folder = images.images_dir(j.settings)
    return sorted(folder.glob("*.png")) if folder.exists() else []


# --------------------------------------------------------------------------- no key: nothing is needed any more
async def test_an_old_openai_choice_with_no_key_never_calls_openai_and_uses_the_claude_designer(tmp_path):
    """Was: 'no key = not connected'. The default is now Claude-designed graphics, which need no key; an old 'openai'
    choice with no key quietly means that, and OpenAI is never contacted."""
    def boom(request):
        raise AssertionError("OpenAI must not be called without a key")

    j = _jarvis(tmp_path, key="", handler=boom, image_provider="openai")
    assert j.images.provider() == "claude" and j.images.configured is True
    assert "not connected" not in j.connections()["Image generation"]
    out = await generate_image(j, ImageIn(headline="Is your fire alarm serviced?", platform="facebook"))
    assert "error" not in out and out["shown_on_display"] is True and out["designer"] in ("claude", "standard")
    assert "not an AI photograph" in out["note"]
    await j.http.aclose()


async def test_unsupported_provider_with_a_key_is_explained(tmp_path):
    j = _jarvis(tmp_path, key=KEY, image_provider="nonsense")
    out = await j.images.generate("Hello", "facebook")
    assert out["connected"] is False and "isn't supported" in out["error"] and "openai" in out["error"]
    assert KEY not in json.dumps(out)
    await j.http.aclose()


def test_tool_is_registered_read_only_and_says_draft_only():
    tool = TOOLS_BY_NAME["generate_image"]
    assert tool.approval is False  # it only ever makes a draft for review
    assert "DRAFT" in tool.description and "never posted" in tool.description
    assert set(tool.model.model_fields) == {"headline", "platform", "subtext", "visual"}


# --------------------------------------------------------------------------- the real path (mocked provider)
async def test_generates_branded_png_shown_on_display_and_saved(tmp_path):
    calls: list = []
    j = _jarvis(tmp_path, key=KEY, handler=_provider_ok(calls))
    q = j.bus.subscribe()
    out = await generate_image(j, ImageIn(headline="Fire alarm servicing in Bradford", platform="instagram",
                                          subtext="BAFE accredited", visual="a smoke detector on a ceiling"))
    assert out["shown_on_display"] is True and out["size"] == "1080x1080" and out["logo"] == "bundled"
    assert out["download_url"] == f"/api/images/{out['image_id']}.png?download=1"
    assert "not been posted" in out["note"]

    files = _stored(j)
    assert [f.stem for f in files] == [out["image_id"]]
    with Image.open(files[0]) as im:
        assert im.format == "PNG" and im.size == (1080, 1080)
        r, g, b = im.convert("RGB").getpixel((5, 1075))  # bottom-left corner = the navy headline panel
        navy = images.NAVY
        assert abs(r - navy[0]) < 25 and abs(g - navy[1]) < 25 and abs(b - navy[2]) < 25

    shown = [e for e in _events(q) if e["type"] == "display"]
    assert len(shown) == 1 and shown[0]["data"]["image_id"] == out["image_id"]
    assert f"![Draft graphic](/api/images/{out['image_id']}.png)" in shown[0]["data"]["markdown"]
    assert "not posted anywhere" in shown[0]["data"]["markdown"]

    # exactly one provider call, key only in the Authorization header, and the headline never leaves the building
    assert len(calls) == 1
    sent = calls[0]
    assert sent.headers["authorization"] == f"Bearer {KEY}" and sent.url.host == "api.openai.com"
    body = json.loads(sent.content)
    assert KEY not in sent.content.decode()
    assert "Bradford" not in body["prompt"] and "smoke detector on a ceiling" in body["prompt"]
    assert "NO people" in body["prompt"] and "NO faces" in body["prompt"] and "NO text" in body["prompt"]
    assert body["model"] == "gpt-image-1" and body["size"] == "1024x1024"
    await j.http.aclose()


@pytest.mark.parametrize("platform", sorted(images.PLATFORM_SIZES))
async def test_each_platform_gets_its_own_size(tmp_path, platform):
    j = _jarvis(tmp_path, key=KEY, handler=_provider_ok([]))
    out = await j.images.generate("Headline " * 9, platform, subtext="Sub text line " * 8)  # 80 chars, within limits
    w, h = images.PLATFORM_SIZES[platform]
    assert out["size"] == f"{w}x{h}"
    with Image.open(_stored(j)[0]) as im:
        assert im.size == (w, h)
    await j.http.aclose()


def test_compose_handles_long_words_and_no_logo():
    png, logo_drawn = images.compose(_png(), "Supercalifragilisticexpialidocious" * 3, "", (1200, 630), None)
    assert logo_drawn is False
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (1200, 630)


# --------------------------------------------------------------------------- failures and secrets
async def test_provider_failure_is_reported_without_the_key_and_nothing_is_made(tmp_path, caplog):
    def refuse(request):
        # a badly behaved provider that echoes the key back in its error body
        return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})

    j = _jarvis(tmp_path, key=KEY, handler=refuse)
    q = j.bus.subscribe()
    with caplog.at_level(logging.DEBUG):
        out = await generate_image(j, ImageIn(headline="Hello"))
    assert "error" in out and "Nothing was created" in out["error"] and "401" in out["error"]
    assert "image_id" not in out
    assert KEY not in json.dumps(out) and KEY not in caplog.text
    assert _stored(j) == [] and _events(q) == []
    await j.http.aclose()


async def test_provider_returning_junk_makes_no_image(tmp_path):
    def junk(request):
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]})

    j = _jarvis(tmp_path, key=KEY, handler=junk)
    out = await j.images.generate("Hello", "linkedin")
    assert "error" in out and "image_id" not in out and _stored(j) == []
    empty = _jarvis(tmp_path / "e", key=KEY, handler=lambda r: httpx.Response(200, json={"data": []}))
    out = await empty.images.generate("Hello", "linkedin")
    assert "error" in out and _stored(empty) == []
    await j.http.aclose()
    await empty.http.aclose()


@pytest.mark.parametrize("kwargs, fragment", [
    ({"headline": "Call us on 01274 123456"}, "phone number"),
    ({"headline": "Email jo@customer.co.uk today"}, "email"),
    ({"headline": "Hello", "subtext": "Visit us at BD1 1AA"}, "postcode"),
    ({"headline": "Hello", "visual": "a portrait of the owner"}, "people or faces"),
    ({"headline": "Hello", "visual": "engineers testing a panel"}, "people or faces"),
    ({"headline": ""}, "headline"),
    ({"headline": "x" * 91}, "under 90"),
])
async def test_customer_details_and_people_are_refused_before_the_provider_is_called(tmp_path, kwargs, fragment):
    calls: list = []
    j = _jarvis(tmp_path, key=KEY, handler=_provider_ok(calls))
    out = await j.images.generate(platform="facebook", **kwargs)
    assert fragment in out["error"] and calls == [] and _stored(j) == []
    await j.http.aclose()


async def test_ordinary_marketing_wording_is_not_refused(tmp_path):
    assert images.check_text("BS 5839 fire alarm servicing", "Q4 2025 offer: 20% off", "a shield and a padlock") == ""


# --------------------------------------------------------------------------- the logo
def test_uploaded_logo_is_validated_and_preferred(tmp_path):
    s = Settings(data_dir=tmp_path, scheduler_enabled=False, _env_file=None)
    assert images.logo_source(s)[0] == "bundled"
    for bad in (b"", b"not an image", b"x" * (images.MAX_LOGO_BYTES + 1)):
        with pytest.raises(ValueError):
            images.save_logo(s, bad)
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    with pytest.raises(ValueError):
        images.save_logo(s, gif.getvalue())
    path = images.save_logo(s, _png((255, 255, 255), (300, 100)))
    assert path.exists() and images.logo_source(s) == ("uploaded", path)
    with Image.open(path) as im:
        assert im.format == "PNG"


async def test_generation_uses_the_uploaded_logo(tmp_path):
    j = _jarvis(tmp_path, key=KEY, handler=_provider_ok([]))
    images.save_logo(j.settings, _png((255, 255, 255), (300, 100)))
    out = await j.images.generate("Hello", "facebook")
    assert out["logo"] == "uploaded" and "uploaded logo added" in out["note"]
    await j.http.aclose()


# --------------------------------------------------------------------------- web routes
def test_image_and_logo_routes_are_owner_only_and_validate(settings):
    settings.jarvis_owner_password = "s3cret"
    j = Jarvis(settings, client=FakeClient())
    folder = images.images_dir(settings)
    folder.mkdir(parents=True)
    (folder / f"{IMAGE_ID}.png").write_bytes(_png())
    app = create_app(settings, j)
    with TestClient(app) as c:
        assert c.get(f"/api/images/{IMAGE_ID}.png").status_code == 401
        assert c.post("/api/brand/logo", files={"logo": ("l.png", _png(), "image/png")}).status_code == 401
        c.post("/login", data={"password": "s3cret"}, follow_redirects=False)

        r = c.get(f"/api/images/{IMAGE_ID}.png")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert "content-disposition" not in r.headers and r.content.startswith(b"\x89PNG")
        r = c.get(f"/api/images/{IMAGE_ID}.png?download=1")
        assert r.headers["content-disposition"] == 'attachment; filename="salts-draft-post-aaaaaaaa.png"'
        for bad in ("nothex.png", f"{'b' * 32}.png", f"{IMAGE_ID}.jpg", f"{IMAGE_ID}"):
            assert c.get(f"/api/images/{bad}").status_code == 404

        assert c.post("/api/brand/logo", files={"logo": ("l.png", b"junk", "image/png")}).status_code == 400
        ok = c.post("/api/brand/logo", files={"logo": ("l.png", _png((255, 255, 255), (200, 80)), "image/png")})
        assert ok.status_code == 200 and ok.json()["saved"] is True
        assert images.logo_source(settings)[0] == "uploaded"


# --------------------------------------------------------------------------- settings and "never posted"
def test_provider_and_key_are_settings_not_hard_coded(tmp_path):
    section = next(s for s in SECTIONS if s.id == "images")
    assert {f.key for f in section.fields} == {"image_provider", "image_api_key", "image_model"}
    assert FIELDS["image_api_key"].kind == "secret"  # encrypted at rest, never sent back to the browser
    assert section.required == ()  # no key is required any more (was: ("image_api_key",)); Claude-designed graphics need none
    assert Settings(_env_file=None, data_dir=tmp_path).image_api_key == ""  # no default key


def test_generator_cannot_post_or_send_anything():
    src = (Path(jarvis.__file__).parent / "services" / "images.py").read_text(encoding="utf-8")
    assert not re.search(r"meta_page_token|linkedin_access_token|tiktok_access_token|instagram_business_id|"
                         r"send_mail|j\.mail|j\.teams|actions\.queue", src)
    assert "image_api_key" in src and src.count('"Authorization"') == 1  # the key only goes in the provider header
    assert not re.search(r"log\.\w+\([^)]*(image_api_key|\bkey\b)", src)
