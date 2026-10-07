"""Records short GIFs of the running app for the features page and the README, by driving a real browser.

Order: read the demo sign-ins from RADREPORT_DEMO_ACCOUNTS (_accounts) -> for each flow, open a
browser that records video and shows a cursor (_CURSOR), act it out (features_tour, demo_sign_in,
admin_panel) -> turn each video into a palette-optimised GIF and a small MP4 with ffmpeg (_to_gif),
written to docs/media. Needs `pip install playwright && playwright install chromium` and ffmpeg.

    python -m radreport.devtools.record_gifs --base-url http://127.0.0.1:8000     (make gifs)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "media"
VIEWPORT = {"width": 1280, "height": 760}

#: A visible pointer and click ripple, since recorded video carries no cursor.
_CURSOR = """
window.addEventListener('DOMContentLoaded', () => {
  const dot = document.createElement('div');
  dot.style.cssText = 'position:fixed;z-index:2147483647;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:50%;background:rgba(79,70,229,.85);box-shadow:0 0 0 4px rgba(79,70,229,.25);pointer-events:none;transition:transform .12s;left:-50px;top:-50px';
  document.body.appendChild(dot);
  document.addEventListener('mousemove', (e) => { dot.style.left = e.clientX + 'px'; dot.style.top = e.clientY + 'px'; }, true);
  document.addEventListener('mousedown', () => { dot.style.transform = 'scale(.7)'; }, true);
  document.addEventListener('mouseup', () => { dot.style.transform = 'none'; }, true);
});
"""


def _accounts() -> dict[str, dict[str, Any]]:
    """The admin-panel and lab demo accounts, by realm."""
    from radreport.core.config import get_settings

    found: dict[str, dict[str, Any]] = {}
    for account in get_settings().demo_accounts:
        found.setdefault("lab" if account.lab else "admin", account.model_dump())
    return found


def _click(page: Any, selector: str, *, pause: int = 500) -> None:
    """Glide the cursor to an element, then click it, so the GIF shows where the click went."""
    box = page.locator(selector).first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)
    page.wait_for_timeout(180)
    page.locator(selector).first.click()
    page.wait_for_timeout(pause)


def _scroll(page: Any, pixels: int, *, pause: int = 400) -> None:
    """Scroll the page itself; wheel events would land on the sticky header and go nowhere."""
    page.evaluate(f"window.scrollBy({{top: {pixels}, behavior: 'smooth'}})")
    page.wait_for_timeout(700 + pause)


def _show(page: Any, selector: str, *, pause: int = 500) -> None:
    """Bring an element to the middle of the screen, smoothly."""
    page.locator(selector).first.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
    page.wait_for_timeout(800 + pause)


def features_tour(page: Any, base: str, _accounts: dict[str, dict[str, Any]]) -> None:
    """The features page's Reason popup, then the recruiter tour's system-design and crash-test tabs."""
    page.goto(f"{base}/features")
    page.wait_for_timeout(900)
    _show(page, ".feature-single .reason-open >> nth=1", pause=300)
    _click(page, ".feature-single .reason-open >> nth=1", pause=1800)
    _click(page, "dialog[open] .reason-close", pause=400)
    page.goto(f"{base}/recruiter")
    page.wait_for_timeout(600)
    # Long enough on the tab bar for the sparkling recordings tab to catch the eye.
    _show(page, ".feature-tabs", pause=1400)
    _click(page, "#tab-btn-hld", pause=1600)
    _scroll(page, 640, pause=900)
    _show(page, ".feature-tabs", pause=0)
    _click(page, "#tab-btn-crash-test", pause=600)
    _scroll(page, 380, pause=1800)


def demo_sign_in(page: Any, base: str, accounts: dict[str, dict[str, Any]]) -> None:
    """The lab sign-in with the test-credentials tab, into the review queue."""
    if "lab" not in accounts:
        raise SystemExit("no lab demo account in RADREPORT_DEMO_ACCOUNTS")
    page.goto(f"{base}/ui/login")
    page.wait_for_timeout(800)
    _click(page, "[role=tab]:has-text('Test credentials')", pause=900)
    _click(page, "[data-fill-email]", pause=700)
    _click(page, "form button[type=submit]", pause=300)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1200)
    _scroll(page, 420, pause=1500)


def admin_panel(page: Any, base: str, accounts: dict[str, dict[str, Any]]) -> None:
    """The admin panel's read-only demo account: labs, then cost and usage."""
    if "admin" not in accounts:
        raise SystemExit("no admin demo account in RADREPORT_DEMO_ACCOUNTS")
    page.goto(f"{base}/admin/login")
    page.wait_for_timeout(700)
    _click(page, "[role=tab]:has-text('Test credentials')", pause=700)
    _click(page, "[data-fill-email]", pause=600)
    _click(page, "form button[type=submit]", pause=300)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1300)
    _click(page, "a[href='/admin/costs']", pause=300)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1300)
    _scroll(page, 500, pause=1600)


FLOWS: dict[str, Callable[[Any, str, dict[str, dict[str, Any]]], None]] = {"features-tour": features_tour, "demo-sign-in": demo_sign_in, "admin-panel": admin_panel}


def _to_gif(video: Path, gif: Path, *, width: int = 880, fps: int = 10) -> None:
    """A two-pass palette GIF for the README, and an MP4 beside it for the web page, a fraction of the size."""
    graph = f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff:max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", graph, "-loop", "0", str(gif)], check=True)
    mp4 = gif.with_suffix(".mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", "fps=24,scale=1280:-2", "-c:v", "libx264", "-crf", "27", "-preset", "slow", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(mp4)], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--only", default="", help="comma-separated flow names: " + ", ".join(FLOWS))
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    args = parser.parse_args(argv)
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is needed: brew install ffmpeg")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is needed: pip install playwright && playwright install chromium")

    accounts = _accounts()
    wanted = [name for name in FLOWS if not args.only or name in args.only.split(",")]
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p, tempfile.TemporaryDirectory() as tmp:
        browser = p.chromium.launch()
        for name in wanted:
            context = browser.new_context(viewport=VIEWPORT, color_scheme=args.theme, record_video_dir=tmp, record_video_size=VIEWPORT)
            context.add_init_script(_CURSOR)
            page = context.new_page()
            FLOWS[name](page, args.base_url.rstrip("/"), accounts)
            video = Path(page.video.path())
            context.close()
            gif = OUT / f"{name}.gif"
            _to_gif(video, gif)
            mp4 = gif.with_suffix(".mp4")
            print(f"  {gif.relative_to(ROOT)}  {gif.stat().st_size / 1_048_576:.1f} MB   {mp4.name}  {mp4.stat().st_size / 1_048_576:.1f} MB")
        browser.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    os.environ.setdefault("RADREPORT_ENVIRONMENT", "local")
    sys.exit(main())
