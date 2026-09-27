"""Standalone Aseer Al Kotb product parser for Egypt-facing prices.

It intentionally ignores gtag, Google Analytics, Facebook Pixel, tracking
payloads, USD analytics values, and all unrelated product/marketing JSON.
The parser reads the server-rendered customer-facing Offer after requesting
country=EG.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag
from playwright.async_api import Page, async_playwright

EGYPT_COUNTRY = "EG"
PRODUCT_NAME_SELECTOR = 'h1[itemprop="name"]'
OFFER_SELECTOR = '[itemprop="offers"][itemscope]'
PRICE_META_SELECTOR = 'meta[itemprop="price"]'
CURRENCY_META_SELECTOR = 'meta[itemprop="priceCurrency"]'
AVAILABILITY_SELECTOR = '[itemprop="availability"]'
NOTIFY_TEXT = "أبلغني عند توفره للشراء"
BUY_TEXT = "شراء نسخة ورقية"
ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
NAVIGATION_TIMEOUT_MS = 25_000
PRODUCT_SELECTOR_TIMEOUT_MS = 8_000


def egypt_url(product_url: str) -> str:
    """Add/update the site's country=EG query parameter without losing others."""
    parts = urlsplit(product_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["country"] = EGYPT_COUNTRY
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_number(value: str | None) -> float | int | None:
    """Parse Arabic/Latin numerals and return a number without currency conversion."""
    text = clean_text(value).translate(ARABIC_DIGITS)
    text = text.replace("٬", ",").replace("٫", ".").replace("،", ",")
    matches = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    if not matches:
        return None
    token = matches[-1].replace(",", "")
    try:
        number = float(token)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def availability_name(href: str | None) -> str | None:
    if not href:
        return None
    value = href.rstrip("/").rsplit("/", 1)[-1].lower()
    return {
        "instock": "In Stock",
        "outofstock": "Out of Stock",
        "soldout": "Out of Stock",
        "preorder": "Pre-Order",
        "backorder": "Back Order",
    }.get(value, value.replace("_", " ").title() or None)


def _labeled_price_rows(offer: Tag) -> dict[str, dict[str, Any]]:
    prices: dict[str, dict[str, Any]] = {}
    for dt in offer.select("dt"):
        label = clean_text(dt.get_text(" ", strip=True)).replace(":", "")
        key = ""
        if label.startswith("قبل"):
            key = "before"
        elif label.startswith("بعد"):
            key = "after"
        elif label.startswith("السعر"):
            key = "price"
        if not key:
            continue
        row = dt.parent
        dd = row.find("dd") if row else None
        if not dd:
            continue
        raw = clean_text(dd.get_text(" ", strip=True))
        value = parse_number(raw)
        if value is not None:
            prices[key] = {"value": value, "raw": raw}
    return prices


def _first_meta_content(offer: Tag, selector: str) -> str | None:
    node = offer.select_one(selector)
    if not node:
        return None
    value = clean_text(node.get("content"))
    return value or None


def _jsonld_egp_offer(soup: BeautifulSoup) -> dict[str, Any] | None:
    """Return an EGP Book/Product offer from JSON-LD, if present."""
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text()
        try:
            data = json.loads(text, strict=False)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            graph = candidate.get("@graph")
            if isinstance(graph, list):
                candidates.extend(item for item in graph if isinstance(item, dict))
            offers = candidate.get("offers")
            offers = offers if isinstance(offers, list) else [offers]
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                if clean_text(str(offer.get("priceCurrency"))) != "EGP":
                    continue
                price = parse_number(str(offer.get("price", "")))
                if price is not None:
                    return {
                        "price": price,
                        "currency": "EGP",
                        "availability": clean_text(str(offer.get("availability", ""))) or None,
                    }
    return None


def parse_product_html(html: str, product_url: str, http_status: int | None = 200) -> dict[str, Any]:
    """Parse one already-fetched Aseer Al Kotb product HTML document.

    Expected source is the main server-rendered product Offer:
      [itemprop="offers"][itemscope]
        dt «قبل» / dd   (optional original price)
        dt «بعد» / dd   (discounted customer price)
        dt «السعر» / dd (no-discount price)
        meta[itemprop="price"]
        meta[itemprop="priceCurrency"] = EGP
        [itemprop="availability"]

    No analytics or tracking element is read.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, Any] = {
        "product_name": clean_text((soup.select_one(PRODUCT_NAME_SELECTOR) or Tag(name="h1")).get_text(" ", strip=True)) or None,
        "product_url": product_url,
        "requested_url": egypt_url(product_url),
        "http_status": http_status,
        "raw_price_source": None,
        "raw_price_value": None,
        "detected_currency": None,
        "price_before_sale": None,
        "price_after_sale": None,
        "currency": "EGP",
        "stock_status": "Unknown",
        "parser_status": "error",
    }

    main = soup.select_one("main") or soup
    offer = main.select_one(OFFER_SELECTOR)
    jsonld_offer = _jsonld_egp_offer(soup)
    main_text = clean_text(main.get_text(" ", strip=True))
    notify = bool(main.select_one(f'a[title="{NOTIFY_TEXT}"], button[title="{NOTIFY_TEXT}"]')) or NOTIFY_TEXT in main_text

    if offer:
        result["detected_currency"] = _first_meta_content(offer, CURRENCY_META_SELECTOR)
        price_meta = _first_meta_content(offer, PRICE_META_SELECTOR)
        availability = offer.select_one(AVAILABILITY_SELECTOR)
        result["stock_status"] = availability_name(availability.get("href")) if availability else "Unknown"

        labeled = _labeled_price_rows(offer)
        before = labeled.get("before")
        after = labeled.get("after")
        no_discount = labeled.get("price")
        is_egypt_offer = result["detected_currency"] == "EGP"
        if is_egypt_offer:
            if before:
                result["price_before_sale"] = before["value"]
            if after:
                result["price_after_sale"] = after["value"]
            elif no_discount:
                result["price_after_sale"] = no_discount["value"]
                result["price_before_sale"] = no_discount["value"]
            elif price_meta is not None:
                result["price_after_sale"] = parse_number(price_meta)
                result["price_before_sale"] = result["price_after_sale"]

        result["raw_price_value"] = {
            "before_text": before["raw"] if before else None,
            "after_text": after["raw"] if after else None,
            "price_label_text": no_discount["raw"] if no_discount else None,
            "meta_price": price_meta,
            "meta_currency": result["detected_currency"],
        }
        if before or after or no_discount or price_meta:
            if before or after:
                result["raw_price_source"] = 'main [itemprop="offers"] dt («قبل»/«بعد») + dd; current meta[itemprop="price"]'
            elif no_discount:
                result["raw_price_source"] = 'main [itemprop="offers"] dt («السعر») + dd; current meta[itemprop="price"]'
            else:
                result["raw_price_source"] = 'main [itemprop="offers"] meta[itemprop="price"]'

        if result["stock_status"] == "Unknown":
            if main.select_one(f'[title="{BUY_TEXT}"]') or (is_egypt_offer and result["price_after_sale"] is not None):
                result["stock_status"] = "In Stock"
    else:
        result["raw_price_source"] = 'main: no [itemprop="offers"][itemscope] block'

    if result["price_after_sale"] is None and jsonld_offer:
        result["price_after_sale"] = jsonld_offer["price"]
        result["price_before_sale"] = jsonld_offer["price"]
        result["detected_currency"] = "EGP"
        result["raw_price_source"] = 'script[type="application/ld+json"] Book.offers.price'
        result["raw_price_value"] = {
            "jsonld_price": jsonld_offer["price"],
            "jsonld_currency": "EGP",
            "jsonld_availability": jsonld_offer.get("availability"),
        }

    if notify:
        result["stock_status"] = "Out of Stock"

    if result["detected_currency"] == "EGP":
        result["parser_status"] = "ok" if result["price_after_sale"] is not None else "out_of_stock_no_price"
    elif result["stock_status"] == "Out of Stock" and not result["detected_currency"]:
        result["parser_status"] = "out_of_stock_no_price"
    elif result["detected_currency"]:
        result["parser_status"] = "currency_not_egp"
    elif result["price_after_sale"] is None:
        result["parser_status"] = "price_not_found"
    else:
        result["parser_status"] = "currency_not_detected"
    return result


async def parse_product_page(page: Page, product_url: str) -> dict[str, Any]:
    """Navigate to country=EG and parse the product's server-rendered HTML."""
    requested_url = egypt_url(product_url)
    try:
        response = await page.goto(
            requested_url,
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        await page.locator(PRODUCT_NAME_SELECTOR).first.wait_for(
            state="attached",
            timeout=PRODUCT_SELECTOR_TIMEOUT_MS,
        )
        html = await page.content()
        result = parse_product_html(html, product_url, response.status if response else None)
        result["requested_url"] = requested_url
        return result
    except Exception as exc:
        return {
            "product_name": None,
            "product_url": product_url,
            "requested_url": requested_url,
            "http_status": None,
            "raw_price_source": None,
            "raw_price_value": None,
            "detected_currency": None,
            "price_before_sale": None,
            "price_after_sale": None,
            "currency": "EGP",
            "stock_status": "Unknown",
            "parser_status": f"error:{type(exc).__name__}",
            "error": str(exc),
        }


async def parse_product_urls(product_urls: list[str], headless: bool = True) -> list[dict[str, Any]]:
    """Parse product URLs sequentially in one browser session."""
    async with async_playwright() as playwright:
        launch_kwargs: dict[str, Any] = {"headless": headless, "args": ["--no-sandbox"]}
        chromium_path = os.getenv("CHROMIUM_PATH")
        if chromium_path and Path(chromium_path).exists():
            launch_kwargs["executable_path"] = chromium_path
        elif Path("/usr/bin/chromium").exists():
            launch_kwargs["executable_path"] = "/usr/bin/chromium"
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(locale="ar-EG", user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        ))
        page = await context.new_page()
        page.set_default_timeout(30_000)
        try:
            rows = []
            for product_url in product_urls:
                rows.append(await parse_product_page(page, product_url))
                await asyncio.sleep(1.0)
            return rows
        finally:
            await browser.close()


if __name__ == "__main__":
    import sys
    urls = [line.strip() for line in sys.stdin if line.strip()]
    print(json.dumps(asyncio.run(parse_product_urls(urls)), ensure_ascii=False, indent=2))
