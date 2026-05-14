"""
Scrape public listing metadata from ekantipur.com entertainment and cartoon pages
using synchronous Playwright: category, article titles, image URLs, authors, and
the featured cartoon caption. Writes structured JSON for downstream use; no
business data is embedded—all article/cartoon fields come from the live DOM.
"""

import json
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


ENTERTAINMENT_URL = "https://ekantipur.com/entertainment"
CARTOON_URL = "https://ekantipur.com/cartoon"


def _category_from_url(page_url: str) -> str:
    """Derive a slug from the URL path when the header link is missing."""
    # Without this guard, malformed URLs or urlparse edge cases would abort the whole scrape.
    try:
        path = urlparse(page_url).path.strip("/")
        segments = [s for s in path.split("/") if s]
        return segments[0] if segments else "unknown"
    except Exception:
        return "unknown"


def _scroll_in_six_steps(page) -> None:
    """Lazy media often loads on scroll; incremental scroll reduces missed assets."""
    # Network or isolated-world failures would leave total unset and crash the scroll loop.
    try:
        total = page.evaluate("() => document.documentElement.scrollHeight")
    except Exception:
        total = 0
    if not total:
        return
    # Images below the fold often use lazy loading; stepping scroll fires intersection observers so src can populate.
    for step in range(6):
        # A single failed scrollTo should not stop later steps from running.
        try:
            y = int((total * step) / 5)
            page.evaluate("(y) => window.scrollTo(0, y)", y)
        except Exception:
            pass
    # Give the browser time to finish decoding and swapping lazy src after the last scroll event.
    try:
        page.wait_for_timeout(2000)
    except Exception:
        # If the page/context closes mid-wait, Playwright can throw; swallow so callers still return partial data.
        pass


def _parse_cartoon_caption(raw: str) -> tuple[str, str | None]:
    """Match site caption patterns: trailing dash vs ' - ' separator vs plain title."""
    text = raw.strip()
    # The markup exposes one paragraph for both fields, so we infer author from punctuation the site uses in that string.
    if text.endswith(" -"):
        return text[:-2].strip(), None
    if " - " in text:
        first, second = text.split(" - ", 1)
        author = second.strip() if second.strip() else None
        return first.strip(), author
    return text, None


def scrape_entertainment(page):
    """Load entertainment listing and return up to five card dicts."""
    # Navigation can fail on DNS, TLS, or timeouts; return empty so the rest of main can still run.
    try:
        page.goto(ENTERTAINMENT_URL, wait_until="domcontentloaded")
    except Exception:
        return []

    # Cards are injected after hydration; reading before this selector appears yields empty or stale locators.
    try:
        page.wait_for_selector("div.category-inner-wrapper", timeout=60000)
    except Exception:
        # Without this catch, a slow or changed layout would bubble and skip JSON entirely.
        return []

    category = None
    # Header category is stable before scroll; capture it first so lazy image churn cannot hide or reorder that node.
    try:
        try:
            el = page.locator("div.category-name p a").first
            category = el.text_content().strip() if el else None
        except Exception:
            # Missing header markup or detached DOM would otherwise abort category resolution.
            category = None
        if not category:
            try:
                category = _category_from_url(page.url)
            except Exception:
                # URL parsing must never prevent emitting rows with a usable fallback label.
                category = "unknown"
    except Exception:
        # Outer guard so any unexpected failure in the nested tries still yields a defined category string.
        category = "unknown"

    _scroll_in_six_steps(page)

    articles = []
    try:
        wrappers = page.locator("div.category-inner-wrapper")
        count = min(5, wrappers.count())
    except Exception:
        # If the locator API errors (closed page), avoid crashing the loop.
        count = 0

    for i in range(count):
        title = None
        image_url = None
        author = None

        try:
            card = wrappers.nth(i)
        except Exception:
            # nth can throw if the collection shrank between count and access.
            continue

        try:
            t_el = card.locator("div.category-description h2 a").first
            title = t_el.text_content().strip() if t_el else None
        except Exception:
            # Optional or reflowed card markup should not drop the entire batch.
            title = None

        try:
            img_el = card.locator("div.category-image figure img").first
            image_url = img_el.get_attribute("src") if img_el else None
        except Exception:
            # Lazy or missing img nodes must not break sibling fields.
            image_url = None

        try:
            a_el = card.locator("div.author-name p a").first
            author = a_el.text_content().strip() if a_el else None
        except Exception:
            # Author blocks are optional per card; missing DOM should map to None, not an exception.
            author = None

        articles.append(
            {
                "title": title,
                "image_url": image_url,
                "category": category,
                "author": author,
            }
        )

    return articles


def scrape_cartoon(page):
    """Load cartoon page and parse the first featured cartoon block."""
    try:
        page.goto(CARTOON_URL, wait_until="domcontentloaded")
    except Exception:
        # Cartoon failure should not crash entertainment output already collected.
        return {"title": None, "image_url": None, "author": None}

    # Cartoon assets render after layout; waiting avoids reading empty wrappers on first paint.
    try:
        page.wait_for_selector("div.cartoon-wrapper", timeout=60000)
    except Exception:
        # Timeout means no DOM to parse; return placeholders instead of raising through main.
        return {"title": None, "image_url": None, "author": None}

    image_url = None
    raw_caption = ""

    try:
        # The page lists multiple cartoons; the task only wants the lead slot, so .first pins that single instance.
        first = page.locator("div.cartoon-wrapper").first
    except Exception:
        first = None

    if first is not None:
        try:
            img_el = first.locator("div.cartoon-image figure img").first
            image_url = img_el.get_attribute("src") if img_el else None
        except Exception:
            # Broken figure markup should still allow caption parsing downstream.
            image_url = None

        try:
            cap_el = first.locator("div.cartoon-description p").first
            raw_caption = cap_el.text_content().strip() if cap_el else ""
        except Exception:
            # Caption optional; empty string keeps parser deterministic.
            raw_caption = ""

    # Split combined caption text into structured fields for JSON consumers.
    title, author = _parse_cartoon_caption(raw_caption)
    return {"title": title, "image_url": image_url, "author": author}


def main() -> None:
    """Drive browser sessions and persist JSON so results are inspectable offline."""
    payload = {
        "entertainment_news": [],
        "cartoon_of_the_day": {"title": None, "image_url": None, "author": None},
    }

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=False)
        except Exception:
            # Missing browser binary or port lock would otherwise leave no graceful exit path.
            return

        try:
            context = browser.new_context()
        except Exception:
            try:
                browser.close()
            except Exception:
                # Close cleanup must not mask the original context failure.
                pass
            return

        try:
            page = context.new_page()
        except Exception:
            try:
                context.close()
            except Exception:
                # Context may already be torn down; ignore so we still attempt browser shutdown.
                pass
            try:
                browser.close()
            except Exception:
                # Browser process may already be gone; swallow to avoid masking the new_page failure.
                pass
            return

        try:
            payload["entertainment_news"] = scrape_entertainment(page)
        except Exception:
            # Unexpected bugs inside scrape_entertainment should not wipe cartoon scraping.
            payload["entertainment_news"] = []

        try:
            payload["cartoon_of_the_day"] = scrape_cartoon(page)
        except Exception:
            # Preserve entertainment rows even if cartoon logic throws.
            payload["cartoon_of_the_day"] = {
                "title": None,
                "image_url": None,
                "author": None,
            }

        try:
            context.close()
        except Exception:
            # Teardown errors are non-fatal once data is gathered.
            pass
        try:
            browser.close()
        except Exception:
            # Double-close or killed driver should not fail the script after JSON is ready.
            pass

    try:
        with open("output.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception:
        # Disk full or permission errors should not raise through __main__.
        pass


if __name__ == "__main__":
    main()
