"""Offline/API regressions for multi-reference validation and memory limits."""
import asyncio
import base64
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def import_app(home):
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("muse_reference_candidate", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


with tempfile.TemporaryDirectory() as home:
    app = import_app(home)
    assert app._image_references(app.ImageRequest(
        prompt="x", images=[{"url": "first"}, {"image_url": {"url": "second"}}])) == ["first", "second"]

    for request in [
        app.ImageRequest(prompt="x", images=[None]),
        app.ImageRequest(prompt="x", images=["a"] * 5),
        app.ImageRequest(prompt="x", reference_image="old", images=["new"]),
    ]:
        try:
            app._image_references(request)
        except app.HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("invalid image references were silently accepted")

    for request in [
        app.VideoRequest(prompt="x", images=[{}]),
        app.VideoRequest(prompt="x", image="old", images=["new"]),
    ]:
        try:
            app._video_references(request)
        except app.HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("invalid video image references were silently accepted")

    engine = app.MuseEngine(SimpleNamespace(profile_dir=home))
    valid = "data:image/png;base64," + base64.b64encode(b"ok").decode()
    assert engine._normalize_image(valid)[1] == "image/png"
    for invalid in [
        "data:text/plain;base64," + base64.b64encode(b"not image").decode(),
        "data:image/png;base64,%%bad%%",
    ]:
        try:
            engine._normalize_image(invalid)
        except app.MuseGenerationError:
            pass
        else:
            raise AssertionError("invalid image payload was accepted")

    oversized = "data:image/png;base64," + base64.b64encode(
        b"x" * (sys.modules[app.MuseEngine.__module__].MAX_REFERENCE_IMAGE_BYTES + 1)).decode()
    try:
        engine._normalize_image(oversized)
    except app.MuseGenerationError as exc:
        assert "10 MiB" in str(exc)
    else:
        raise AssertionError("oversized image was accepted")

    try:
        engine._attach_images([valid] * 5)
    except app.MuseGenerationError as exc:
        assert "最多 4 张" in str(exc)
    else:
        raise AssertionError("more than four images were accepted")

print("PASS: reference count, item, conflict, MIME and byte limits")
