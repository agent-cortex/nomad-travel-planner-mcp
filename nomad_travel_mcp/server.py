from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote_plus

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("nomad-travel-planner")

AMADEUS_TEST_BASE = "https://test.api.amadeus.com"
AMADEUS_PROD_BASE = "https://api.amadeus.com"
BOOKING_PROD_BASE = "https://demandapi.booking.com/3.1"
BOOKING_SANDBOX_BASE = "https://demandapi-sandbox.booking.com/3.1"
TRIPADVISOR_BASE = "https://api.content.tripadvisor.com/api/v1"
DEFAULT_TIMEOUT = float(os.getenv("NOMAD_TRAVEL_TIMEOUT", "25"))
CACHE_TTL = int(os.getenv("NOMAD_TRAVEL_CACHE_TTL_SECONDS", "900"))
USER_AGENT = os.getenv(
    "NOMAD_TRAVEL_USER_AGENT",
    "nomad-travel-planner-mcp/0.1 (+https://modelcontextprotocol.io)",
)

_cache: dict[str, tuple[float, Any]] = {}
_amadeus_token: tuple[float, str] | None = None


def _now() -> float:
    return time.time()


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _cache_get(key: str) -> Any | None:
    item = _cache.get(key)
    if not item:
        return None
    ts, value = item
    if _now() - ts > CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> Any:
    _cache[key] = (_now(), value)
    return value


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})


def _amadeus_base() -> str:
    return AMADEUS_PROD_BASE if _env("AMADEUS_ENV") == "production" else AMADEUS_TEST_BASE


def _browser_mode(explicit: str | None = None) -> str:
    return (explicit or _env("NOMAD_TRAVEL_BROWSER_MODE") or "auto").strip().lower()


def _browser_engine_status() -> dict[str, Any]:
    return {
        "mode_default": _browser_mode(),
        "browser_use_cli": bool(shutil.which("browser-use")),
        "browserbase_configured": bool(_env("BROWSERBASE_API_KEY") or _env("BROWSER_USE_API_KEY")),
        "note": "auto tries browser-use CLI when installed; otherwise tools return browser-first task payloads and then static/API fallback results.",
    }


async def _run_command(args: list[str], timeout: int = 45) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"Timed out after {timeout}s: {' '.join(args)}"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def _browseruse_get_html(url: str, wait_selector: str = "body", timeout: int = 90) -> dict[str, Any]:
    binary = shutil.which("browser-use")
    if not binary:
        return {"ok": False, "engine": "browser-use", "error": "browser-use CLI not installed"}
    commands = [
        [binary, "open", url],
        [binary, "wait", "selector", wait_selector, "--timeout", "20000"],
        [binary, "get", "html", "--selector", "body"],
    ]
    logs: list[dict[str, Any]] = []
    remaining = timeout
    for cmd in commands:
        start = _now()
        code, out, err = await _run_command(cmd, timeout=max(10, remaining))
        logs.append({"cmd": " ".join(cmd), "exit_code": code, "stderr": err[-500:]})
        remaining -= int(_now() - start)
        if code != 0:
            return {"ok": False, "engine": "browser-use", "error": err or out or f"exit {code}", "logs": logs}
        if cmd[-3:] == ["html", "--selector", "body"] or (len(cmd) >= 3 and cmd[1:3] == ["get", "html"]):
            return {"ok": True, "engine": "browser-use", "html": out, "logs": logs}
    return {"ok": False, "engine": "browser-use", "error": "No html returned", "logs": logs}


def _browser_first_task_payload(kind: str, url: str, task: str, output_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": kind,
        "target_url": url,
        "primary_strategy": "browser-first",
        "browser_use_cli": [
            f"browser-use open {json.dumps(url)}",
            "browser-use wait selector \"body\" --timeout 20000",
            "browser-use get html --selector \"body\"",
        ],
        "browser_use_cloud_cli": [
            "browser-use cloud connect",
            f"browser-use open {json.dumps(url)}",
            "browser-use wait selector \"body\" --timeout 20000",
            "browser-use get html --selector \"body\"",
        ],
        "browser_use_agent_task": task,
        "browserbase_task": {"startUrl": url, "task": task, "outputSchema": output_schema},
        "rules": [
            "Use only visible/public data unless the user explicitly authenticates in their own browser profile.",
            "Do not bypass bot checks, solve CAPTCHAs, or scrape private/paywalled data.",
            "For signup/login, use AgentMail for email codes/magic links; stop only for CAPTCHA, payment, passwords, or explicit human approval gates.",
        ],
    }


def _agentmail_default_inbox() -> str:
    return _env("AGENTMAIL_INBOX_ID") or _env("NOMAD_TRAVEL_AGENTMAIL_INBOX") or "agent_cortex@agentmail.to"


def _agentmail_status() -> dict[str, Any]:
    return {
        "configured": bool(_env("AGENTMAIL_API_KEY")),
        "default_inbox_id": _agentmail_default_inbox(),
        "sdk_optional": "Install agentmail Python SDK or provide AGENTMAIL_API_KEY in the MCP server environment to poll verification codes.",
    }


def _agentmail_auth_task(site: str, email: str) -> str:
    return (
        f"Use {email} as the signup/login email for {site}. If the site sends a magic link or verification code, "
        "call the MCP tool agentmail_latest_verification_code with this inbox and a site-specific hint, then enter the code/link in the browser. "
        "Only stop for CAPTCHA, payment, or explicit human approval gates. Never ask the user to paste OTPs or passwords into chat."
    )


async def _agentmail_latest_code(inbox_id: str, subject_hint: str | None = None, from_hint: str | None = None, minutes: int = 20) -> dict[str, Any]:
    api_key = _env("AGENTMAIL_API_KEY")
    if not api_key:
        return {
            "configured": False,
            "inbox_id": inbox_id,
            "error": "AGENTMAIL_API_KEY is not configured in the MCP server environment",
            "setup_hint": "Set AGENTMAIL_API_KEY and optionally AGENTMAIL_INBOX_ID/NOMAD_TRAVEL_AGENTMAIL_INBOX. Do not paste the key into chat.",
        }
    try:
        from agentmail import AgentMail  # type: ignore
    except Exception as exc:
        return {
            "configured": True,
            "inbox_id": inbox_id,
            "error": f"agentmail Python SDK unavailable: {exc}",
            "setup_hint": "Install with: pip install agentmail",
        }

    def fetch() -> dict[str, Any]:
        from datetime import datetime, timedelta, timezone
        client = AgentMail(api_key=api_key)
        after = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        resp = client.inboxes.messages.list(inbox_id=inbox_id, after=after, limit=20)
        messages = getattr(resp, "messages", [])
        candidates: list[dict[str, Any]] = []
        for msg in messages:
            subject = getattr(msg, "subject", "") or ""
            sender = str(getattr(msg, "from_", "") or "")
            body = (
                getattr(msg, "extracted_text", None)
                or getattr(msg, "text", None)
                or getattr(msg, "extracted_html", None)
                or getattr(msg, "html", None)
                or ""
            )
            haystack = f"{subject}\n{sender}\n{body}".lower()
            if subject_hint and subject_hint.lower() not in haystack:
                continue
            if from_hint and from_hint.lower() not in sender.lower() and from_hint.lower() not in haystack:
                continue
            code_match = re.search(r"(?<!\d)(\d{4,8})(?!\d)", body) or re.search(r"(?<!\d)(\d{4,8})(?!\d)", subject)
            link_match = re.search(r"https?://[^\s<>\"']+", body)
            candidates.append(
                {
                    "subject": subject,
                    "from": sender,
                    "created_at": str(getattr(msg, "created_at", "") or ""),
                    "code": code_match.group(1) if code_match else None,
                    "link": link_match.group(0) if link_match else None,
                }
            )
        return {
            "configured": True,
            "inbox_id": inbox_id,
            "subject_hint": subject_hint,
            "from_hint": from_hint,
            "minutes": minutes,
            "count": len(candidates),
            "latest": candidates[0] if candidates else None,
            "candidates": candidates[:5],
            "warning": "Verification codes/magic links are returned to the host agent for browser entry only; do not print them to end-user chat logs.",
        }

    return await asyncio.to_thread(fetch)


def _signup_assistance_payload(site: str, signup_url: str, inbox_id: str | None = None) -> dict[str, Any]:
    inbox = inbox_id or _agentmail_default_inbox()
    auth_task = (
        f"Open {signup_url}. Sign up or log in to {site} using the AgentMail inbox {inbox}. "
        f"{_agentmail_auth_task(site, inbox)} "
        "If no email arrives, retry once after 30 seconds, then report the blocker. Stop for CAPTCHA, payment, paid subscription, or terms-sensitive approval gates."
    )
    return {
        "site": site,
        "signup_url": signup_url,
        "primary_email_strategy": "agentmail",
        "agentmail": {
            **_agentmail_status(),
            "inbox_id": inbox,
            "poll_tool": "agentmail_latest_verification_code",
            "poll_args": {"inbox_id": inbox, "subject_hint": site, "minutes": 20},
        },
        "recommended_flow": "Use browser-use/Browserbase to enter the AgentMail address, then poll AgentMail for magic links/codes. Human interaction is only needed for CAPTCHA, payment, or explicit approval gates.",
        "browser_use_cli": [
            f"browser-use --headed open {json.dumps(signup_url)}",
            "browser-use state",
            f"# Enter AgentMail inbox when email is requested: {inbox}",
            f"# Then call MCP tool agentmail_latest_verification_code(inbox_id={json.dumps(inbox)}, subject_hint={json.dumps(site)})",
        ],
        "browser_use_profile_cli": [
            f"browser-use --profile \"Default\" open {json.dumps(signup_url)}",
            "browser-use state",
            f"# Enter AgentMail inbox when email is requested: {inbox}",
        ],
        "browserbase_task": {
            "startUrl": signup_url,
            "task": auth_task,
        },
        "hard_rules": [
            "Use AgentMail for email verification; do not ask the user to manually read/paste OTPs or magic links.",
            "Never ask the user to paste passwords, API keys, payment details, or private account secrets into chat.",
            "Never automate CAPTCHA/anti-bot bypass.",
            "Never create paid bookings, paid subscriptions, or affiliate commitments without explicit user approval.",
        ],
    }


async def _amadeus_access_token() -> str:
    global _amadeus_token
    if _amadeus_token and _amadeus_token[0] > _now() + 60:
        return _amadeus_token[1]
    client_id = _env("AMADEUS_CLIENT_ID")
    client_secret = _env("AMADEUS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("Missing AMADEUS_CLIENT_ID/AMADEUS_CLIENT_SECRET")
    async with _client() as client:
        r = await client.post(
            f"{_amadeus_base()}/v1/security/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        data = r.json()
    _amadeus_token = (_now() + int(data.get("expires_in", 1800)), data["access_token"])
    return _amadeus_token[1]


async def _amadeus_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    token = await _amadeus_access_token()
    async with _client() as client:
        r = await client.get(
            f"{_amadeus_base()}{path}",
            params={k: v for k, v in params.items() if v is not None and v != ""},
            headers={"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.json()


async def _booking_post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    token = _env("BOOKING_API_TOKEN")
    affiliate_id = _env("BOOKING_AFFILIATE_ID")
    if not token or not affiliate_id:
        raise RuntimeError("Missing BOOKING_API_TOKEN/BOOKING_AFFILIATE_ID")
    base = BOOKING_SANDBOX_BASE if _env("BOOKING_ENV") == "sandbox" else BOOKING_PROD_BASE
    async with _client() as client:
        r = await client.post(
            f"{base}{path}",
            json=body,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Affiliate-Id": affiliate_id,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        r.raise_for_status()
        return r.json()


def _price_amount(price: dict[str, Any] | None) -> float | None:
    if not price:
        return None
    raw = price.get("total") or price.get("grandTotal") or price.get("base")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_flight_offer(offer: dict[str, Any], dictionaries: dict[str, Any]) -> dict[str, Any]:
    price = offer.get("price", {})
    itineraries = []
    carriers = dictionaries.get("carriers", {}) if dictionaries else {}
    for itin in offer.get("itineraries", []):
        segments = []
        for seg in itin.get("segments", []):
            carrier = seg.get("carrierCode")
            segments.append(
                {
                    "from": seg.get("departure", {}).get("iataCode"),
                    "to": seg.get("arrival", {}).get("iataCode"),
                    "departure": seg.get("departure", {}).get("at"),
                    "arrival": seg.get("arrival", {}).get("at"),
                    "carrier": carrier,
                    "carrier_name": carriers.get(carrier, carrier),
                    "flight": f"{carrier or ''}{seg.get('number', '')}",
                    "duration": seg.get("duration"),
                    "stops": len(seg.get("co2Emissions", [])),
                }
            )
        itineraries.append({"duration": itin.get("duration"), "segments": segments})
    return {
        "id": offer.get("id"),
        "source": "amadeus",
        "instant_ticketing_required": offer.get("instantTicketingRequired"),
        "one_way": offer.get("oneWay"),
        "last_ticketing_date": offer.get("lastTicketingDate"),
        "currency": price.get("currency"),
        "total": _price_amount(price),
        "base": _safe_float(price.get("base")),
        "itineraries": itineraries,
        "raw_offer_ref": offer.get("id"),
    }


def _score_accommodation(item: dict[str, Any], nightly_budget: float | None, quality_weight: float) -> float:
    price = item.get("nightly_price") or item.get("total_price") or 0
    rating = item.get("rating") or item.get("review_score") or 0
    review_count = item.get("review_count") or 0
    budget_score = 1.0
    if nightly_budget and price:
        budget_score = max(0.0, min(1.2, nightly_budget / price))
    quality_score = (float(rating) / 5.0) if rating and rating <= 5 else (float(rating) / 10.0 if rating else 0.5)
    review_score = min(1.0, math.log10(review_count + 1) / 4.0) if review_count else 0.2
    return round((budget_score * (1 - quality_weight)) + (quality_score * quality_weight) + review_score * 0.15, 4)


def _extract_json_ld(html: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for match in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.I | re.S):
        try:
            data = json.loads(match.strip())
            if isinstance(data, list):
                out.extend([x for x in data if isinstance(x, dict)])
            elif isinstance(data, dict):
                out.append(data)
        except json.JSONDecodeError:
            continue
    return out


async def _scrape_nomads_city(city: str) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9]+", "-", city.lower()).strip("-")
    urls = [f"https://nomads.com/{slug}", f"https://nomads.com/cost-of-living/{slug}"]
    async with _client() as client:
        for url in urls:
            try:
                r = await client.get(url)
                if r.status_code >= 400 or not r.text:
                    continue
                text = re.sub(r"\s+", " ", r.text)
                costs = [float(x.replace(",", "")) for x in re.findall(r"\$\s*([0-9][0-9,]{1,6})(?:\s*/\s*mo| per month|/month)?", text)]
                internet = [float(x) for x in re.findall(r"([0-9]{2,4}(?:\.[0-9]+)?)\s*Mbps", text, re.I)]
                nomad_cost = None
                cost_meta = re.search(r'twitter:label2[^>]+value=["\']Nomad Cost[^>]+twitter:data2[^>]+value=["\']\$?([0-9,]+)', text, re.I)
                if cost_meta:
                    nomad_cost = float(cost_meta.group(1).replace(",", ""))
                json_ld = _extract_json_ld(r.text)
                title = re.search(r"<title>(.*?)</title>", r.text, re.I | re.S)
                plausible_monthly_costs = [x for x in costs if 300 <= x <= 10000]
                return {
                    "source": "nomads.com_scrape",
                    "url": str(r.url),
                    "title": re.sub(r"\s+", " ", title.group(1)).strip() if title else None,
                    "cost_usd_month_candidates": sorted(set(plausible_monthly_costs))[:10],
                    "estimated_cost_usd_month": nomad_cost or (plausible_monthly_costs[0] if plausible_monthly_costs else None),
                    "internet_mbps_candidates": sorted(set(internet), reverse=True)[:5],
                    "estimated_internet_mbps": max(internet) if internet else None,
                    "json_ld_types": [x.get("@type") for x in json_ld if x.get("@type")],
                    "confidence": "medium" if costs or internet else "low",
                    "warning": "Scraped public page; selectors can break and site terms/robots should be respected for production.",
                }
            except Exception:
                continue
    fallback_url = f"https://nomads.com/{slug}"
    return {
        "source": "nomads.com_scrape",
        "city": city,
        "error": "No readable Nomads.com page found",
        "browser_fallback": {
            "reason": "Static HTTP scrape failed or was blocked.",
            "target_url": fallback_url,
            "browser_use_cli": [
                f"browser-use open {json.dumps(fallback_url)}",
                "browser-use wait selector \"body\" --timeout 15000",
                "browser-use get html --selector \"body\"",
            ],
            "browser_use_agent_task": f"Open {fallback_url}. Extract nomad cost/month, internet speed, safety/weather/quality hints, and return compact JSON.",
            "browserbase_task": {
                "startUrl": fallback_url,
                "task": "Extract digital nomad city signals: cost per month, internet speed, safety/weather/quality hints. Return JSON only.",
            },
        },
    }


def _strip_html(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"&amp;", "&", value)
    value = re.sub(r"&#x27;", "'", value)
    value = re.sub(r"&quot;", '"', value)
    value = re.sub(r"\s+", " ", value)
    return value.strip() or None


def _tripadvisor_browser_fallback(url: str, query: str | None = None) -> dict[str, Any]:
    task = (
        f"Open {url}. Extract the place/hotel name, rating, review count, ranking, address, "
        "price level, amenities/stay-quality hints, and current visible listing snippets. "
        "Return JSON only with fields: name, rating, review_count, ranking, address, price_level, "
        "url, snippets, warnings. Do not log in or bypass bot checks."
    )
    return {
        "reason": "Static HTTP scrape did not produce enough data. Tripadvisor often renders/blocks content dynamically.",
        "target_url": url,
        "query": query,
        "browser_use_cli": [
            f"browser-use open {json.dumps(url)}",
            "browser-use wait selector \"body\" --timeout 15000",
            "browser-use get html --selector \"body\"",
        ],
        "browser_use_agent_task": task,
        "browserbase_task": {
            "startUrl": url,
            "task": task,
            "outputSchema": {
                "name": "string|null",
                "rating": "number|null",
                "review_count": "integer|null",
                "ranking": "string|null",
                "address": "string|null",
                "price_level": "string|null",
                "url": "string",
                "snippets": ["string"],
                "warnings": ["string"],
            },
        },
    }


def _extract_tripadvisor_from_html(html: str, url: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", html)
    json_ld = _extract_json_ld(html)
    title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = _strip_html(title_match.group(1) if title_match else None)
    meta_desc = None
    meta = re.search(r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']', html, re.I | re.S)
    if meta:
        meta_desc = _strip_html(meta.group(1))

    merged: dict[str, Any] = {}
    for item in json_ld:
        if not isinstance(item, dict):
            continue
        typ = item.get("@type")
        if isinstance(typ, list):
            typ = ",".join(str(x) for x in typ)
        if typ and any(k in str(typ).lower() for k in ["hotel", "lodging", "restaurant", "touristattraction", "localbusiness", "place"]):
            merged.update(item)
            break
    if not merged and json_ld:
        merged = json_ld[0]

    rating = None
    review_count = None
    aggregate = merged.get("aggregateRating") if isinstance(merged.get("aggregateRating"), dict) else {}
    rating = _safe_float(aggregate.get("ratingValue"))
    review_count = int(_safe_float(aggregate.get("reviewCount") or aggregate.get("ratingCount")) or 0) or None
    if rating is None:
        m = re.search(r'"ratingValue"\s*:\s*"?([0-9.]+)', text)
        rating = _safe_float(m.group(1)) if m else None
    if review_count is None:
        m = re.search(r'"(?:reviewCount|ratingCount)"\s*:\s*"?([0-9,]+)', text)
        if m:
            review_count = int(float(m.group(1).replace(",", "")))
    if review_count is None:
        m = re.search(r'([0-9][0-9,]*)\s+reviews?', text, re.I)
        if m:
            review_count = int(m.group(1).replace(",", ""))

    ranking = None
    m = re.search(r'(#\s*[0-9,]+\s+of\s+[0-9,]+\s+[^<"|]{3,80})', text, re.I)
    if m:
        ranking = _strip_html(m.group(1))

    address = None
    raw_address = merged.get("address")
    if isinstance(raw_address, dict):
        parts = [raw_address.get(k) for k in ["streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"]]
        address = ", ".join(str(x) for x in parts if x)
    elif isinstance(raw_address, str):
        address = raw_address

    price_level = merged.get("priceRange")
    if not price_level:
        m = re.search(r'"priceRange"\s*:\s*"([^"\\]+)', text)
        price_level = m.group(1) if m else None

    name = merged.get("name") or (title.split(" - ")[0] if title else None)
    snippets = []
    for pattern in [r'(.{0,80}[0-9.]+\s+of\s+5\s+bubbles.{0,80})', r'(.{0,80}#\s*[0-9,]+\s+of\s+[0-9,]+.{0,80})', r'(.{0,80}(?:Excellent|Very good|Average|Poor|Terrible).{0,80})']:
        for match in re.findall(pattern, text, re.I):
            clean = _strip_html(match)
            if clean and clean not in snippets:
                snippets.append(clean)
            if len(snippets) >= 8:
                break
        if len(snippets) >= 8:
            break

    confidence = "high" if rating and review_count else "medium" if name or title or meta_desc else "low"
    return {
        "source": "tripadvisor_static_scrape",
        "url": url,
        "name": name,
        "title": title,
        "description": meta_desc,
        "rating": rating,
        "review_count": review_count,
        "ranking": ranking,
        "address": address,
        "price_level": price_level,
        "json_ld_types": [x.get("@type") for x in json_ld if isinstance(x, dict) and x.get("@type")],
        "snippets": snippets[:8],
        "confidence": confidence,
        "warning": "Static scrape of public Tripadvisor page; selectors can break and Tripadvisor may block/dynamically render content.",
    }


async def _scrape_tripadvisor_public(query_or_url: str, category: str = "Hotels") -> dict[str, Any]:
    if query_or_url.startswith("http://") or query_or_url.startswith("https://"):
        urls = [query_or_url]
        query = None
    else:
        query = query_or_url
        urls = [f"https://www.tripadvisor.com/Search?q={quote_plus(query_or_url)}&searchSessionId=nomad-travel-planner"]
    async with _client() as client:
        last_error = None
        for url in urls:
            try:
                r = await client.get(
                    url,
                    headers={
                        "User-Agent": _env("NOMAD_TRAVEL_BROWSER_UA") or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                if r.status_code >= 400:
                    last_error = f"HTTP {r.status_code}"
                    continue
                result = _extract_tripadvisor_from_html(r.text, str(r.url))
                result["query"] = query_or_url
                result["category_hint"] = category
                if result.get("confidence") != "low":
                    return result
                result["browser_fallback"] = _tripadvisor_browser_fallback(str(r.url), query)
                return result
            except Exception as exc:
                last_error = str(exc)
    fallback_url = urls[0]
    return {
        "source": "tripadvisor_static_scrape",
        "url": fallback_url,
        "query": query_or_url,
        "category_hint": category,
        "error": last_error or "No readable Tripadvisor page found",
        "confidence": "low",
        "browser_fallback": _tripadvisor_browser_fallback(fallback_url, None if query_or_url.startswith("http") else query_or_url),
    }



def _accommodation_browser_fallback(source: str, url: str, preferences: dict[str, Any]) -> dict[str, Any]:
    task = (
        f"Open {url}. Extract visible public accommodation listings from {source}. "
        "For each listing capture name, nightly price, total price if visible, currency, rating, review count, room/property type, "
        "location/area, cancellation/fees if visible, amenities relevant to the preferences, URL, and short warnings. "
        f"User preferences: {json.dumps(preferences, ensure_ascii=False)}. "
        "Return JSON only. Do not log in, bypass bot checks, solve CAPTCHAs, or scrape private data."
    )
    return {
        "reason": "Browser-first accommodation extraction requested or static HTTP scrape failed/was blocked/returned low-confidence data.",
        "target_url": url,
        "source": source,
        "browser_use_cli": [
            f"browser-use open {json.dumps(url)}",
            "browser-use wait selector \"body\" --timeout 20000",
            "browser-use get html --selector \"body\"",
        ],
        "browser_use_agent_task": task,
        "browserbase_task": {
            "startUrl": url,
            "task": task,
            "outputSchema": {
                "source": "string",
                "listings": [
                    {
                        "name": "string|null",
                        "nightly_price": "number|null",
                        "total_price": "number|null",
                        "currency": "string|null",
                        "rating": "number|null",
                        "review_count": "integer|null",
                        "property_type": "string|null",
                        "area": "string|null",
                        "amenities": ["string"],
                        "url": "string|null",
                        "preference_match_notes": ["string"],
                        "warnings": ["string"],
                    }
                ],
                "warnings": ["string"],
            },
        },
    }


def _build_public_accommodation_url(source: str, city: str, country: str | None, checkin: str, checkout: str, adults: int, rooms: int) -> str:
    where = f"{city}, {country}" if country else city
    q = quote_plus(where)
    source = source.lower().strip()
    if source in {"airbnb", "airbnb.com"}:
        return f"https://www.airbnb.com/s/{quote_plus(where)}/homes?checkin={checkin}&checkout={checkout}&adults={adults}&search_type=filter_change"
    if source in {"booking", "booking.com"}:
        return f"https://www.booking.com/searchresults.html?ss={q}&checkin={checkin}&checkout={checkout}&group_adults={adults}&no_rooms={rooms}&group_children=0"
    if source in {"hostelworld", "hostelworld.com"}:
        return f"https://www.hostelworld.com/s?q={q}&dateFrom={checkin}&dateTo={checkout}&guests={adults}"
    if source in {"agoda", "agoda.com"}:
        return f"https://www.agoda.com/search?city={q}&checkIn={checkin}&checkOut={checkout}&adults={adults}&rooms={rooms}"
    return f"https://www.google.com/search?q={quote_plus(source + ' accommodation ' + where + ' ' + checkin + ' ' + checkout)}"


def _currency_from_text(text: str, default: str = "USD") -> str:
    if "₹" in text or "INR" in text:
        return "INR"
    if "€" in text or "EUR" in text:
        return "EUR"
    if "£" in text or "GBP" in text:
        return "GBP"
    if "$" in text or "USD" in text:
        return "USD"
    return default


def _extract_price_candidates(text: str) -> list[float]:
    prices: list[float] = []
    patterns = [
        r'(?:₹|INR\s*)\s*([0-9][0-9,]{1,8})',
        r'(?:\$|USD\s*)\s*([0-9][0-9,]{1,7})',
        r'(?:€|EUR\s*)\s*([0-9][0-9,]{1,7})',
        r'(?:£|GBP\s*)\s*([0-9][0-9,]{1,7})',
    ]
    for pat in patterns:
        for raw in re.findall(pat, text, re.I):
            try:
                val = float(raw.replace(',', ''))
                if 3 <= val <= 500000:
                    prices.append(val)
            except ValueError:
                pass
    return sorted(set(prices))[:20]


def _extract_rating_candidates(text: str) -> list[float]:
    ratings: list[float] = []
    for pat in [r'([0-9](?:\.[0-9])?)\s*(?:/\s*10|out of 10)', r'([0-5](?:\.[0-9])?)\s*(?:/\s*5|out of 5)']:
        for raw in re.findall(pat, text, re.I):
            val = _safe_float(raw)
            if val is not None:
                ratings.append(val)
    return sorted(set(ratings), reverse=True)[:10]


def _score_preference_match(item: dict[str, Any], preferences: dict[str, Any]) -> float:
    text = " ".join(str(x).lower() for x in [item.get("name"), item.get("property_type"), item.get("description"), " ".join(item.get("snippets", []))])
    score = 0.5
    nightly_budget = preferences.get("nightly_budget")
    price = item.get("nightly_price") or item.get("total_price")
    if nightly_budget and price:
        score += min(0.3, max(-0.25, (float(nightly_budget) - float(price)) / max(float(nightly_budget), 1) * 0.3))
    for term in preferences.get("must_haves", []) or []:
        t = str(term).lower()
        if t in text:
            score += 0.08
        elif t in {"clean bathroom", "private bathroom", "desk", "wifi", "air conditioning", "ac"}:
            score -= 0.04
    for term in preferences.get("nice_to_haves", []) or []:
        if str(term).lower() in text:
            score += 0.04
    for term in preferences.get("avoid", []) or []:
        if str(term).lower() in text:
            score -= 0.12
    wanted_types = [str(x).lower() for x in (preferences.get("accommodation_types") or [])]
    if wanted_types and any(t in text for t in wanted_types):
        score += 0.12
    if preferences.get("private_room") and any(t in text for t in ["private", "entire", "apartment", "studio", "hotel"]):
        score += 0.1
    if preferences.get("avoid_hostels") and any(t in text for t in ["dorm", "hostel", "shared"]):
        score -= 0.2
    rating = item.get("rating")
    if rating:
        score += min(0.2, (float(rating) / (10 if float(rating) > 5 else 5)) * 0.2)
    return round(max(0.0, min(1.2, score)), 4)


def _extract_public_accommodation_from_html(source: str, html: str, url: str, preferences: dict[str, Any]) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", html)
    title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = _strip_html(title_match.group(1) if title_match else None)
    json_ld = _extract_json_ld(html)
    prices = _extract_price_candidates(text)
    ratings = _extract_rating_candidates(text)
    currency = _currency_from_text(text, preferences.get("currency", "USD"))
    snippets: list[str] = []
    snippet_patterns = [
        r'(.{0,70}(?:private room|entire|studio|apartment|hotel|hostel|dorm|villa).{0,90})',
        r'(.{0,70}(?:clean|bathroom|wifi|wi-fi|desk|air conditioning|AC|kitchen).{0,90})',
        r'(.{0,70}(?:[0-9.]+\s*(?:/\s*10|out of 10|/\s*5|out of 5)).{0,90})',
    ]
    for pat in snippet_patterns:
        for raw in re.findall(pat, text, re.I):
            clean = _strip_html(raw)
            if clean and clean not in snippets:
                snippets.append(clean)
            if len(snippets) >= 10:
                break
        if len(snippets) >= 10:
            break

    listings: list[dict[str, Any]] = []
    for item in json_ld:
        if not isinstance(item, dict):
            continue
        typ = item.get("@type")
        typ_text = " ".join(typ) if isinstance(typ, list) else str(typ or "")
        if not any(k in typ_text.lower() for k in ["hotel", "lodging", "hostel", "accommodation", "apartment", "house", "product", "listitem"]):
            continue
        offer = item.get("offers") if isinstance(item.get("offers"), dict) else {}
        aggregate = item.get("aggregateRating") if isinstance(item.get("aggregateRating"), dict) else {}
        price = _safe_float(offer.get("price") or item.get("price"))
        listing = {
            "source": source,
            "name": item.get("name") or title,
            "property_type": typ_text or None,
            "description": _strip_html(item.get("description")) if isinstance(item.get("description"), str) else None,
            "currency": offer.get("priceCurrency") or currency,
            "nightly_price": price,
            "total_price": None,
            "rating": _safe_float(aggregate.get("ratingValue")),
            "review_count": int(_safe_float(aggregate.get("reviewCount") or aggregate.get("ratingCount")) or 0) or None,
            "url": item.get("url") or url,
            "snippets": snippets[:5],
        }
        listing["preference_score"] = _score_preference_match(listing, preferences)
        listings.append(listing)
        if len(listings) >= int(preferences.get("max_results", 10)):
            break

    if not listings and (prices or ratings or snippets or title):
        listing = {
            "source": source,
            "name": title,
            "property_type": None,
            "description": None,
            "currency": currency,
            "nightly_price": prices[0] if prices else None,
            "total_price": None,
            "rating": ratings[0] if ratings else None,
            "review_count": None,
            "url": url,
            "snippets": snippets[:8],
        }
        listing["preference_score"] = _score_preference_match(listing, preferences)
        listings.append(listing)

    confidence = "high" if listings and any(x.get("nightly_price") and x.get("rating") for x in listings) else "medium" if listings else "low"
    result = {
        "source": f"{source}_static_scrape",
        "url": url,
        "title": title,
        "count": len(listings),
        "listings": sorted(listings, key=lambda x: (-x.get("preference_score", 0), x.get("nightly_price") or 10**9)),
        "price_candidates": prices,
        "rating_candidates": ratings,
        "confidence": confidence,
        "warning": f"Best-effort public {source} scrape. Prefer official APIs when available; browser fallback may be needed for dynamic pages.",
    }
    if confidence == "low":
        result["browser_fallback"] = _accommodation_browser_fallback(source, url, preferences)
    return result


async def _browser_first_accommodation_site(source: str, city: str, country: str | None, checkin: str, checkout: str, adults: int, rooms: int, preferences: dict[str, Any], browser_mode: str | None = None) -> dict[str, Any]:
    url = _build_public_accommodation_url(source, city, country, checkin, checkout, adults, rooms)
    mode = _browser_mode(browser_mode)
    task_payload = _accommodation_browser_fallback(source, url, preferences)
    if mode in {"task", "task-only", "browserbase", "browser-use-cloud"}:
        return {
            "source": f"{source}_browser_first_task",
            "url": url,
            "count": 0,
            "listings": [],
            "confidence": "low",
            "browser_first": task_payload,
            "fallback_next": "static_scrape",
            "note": "Host agent should execute browser_first via browser-use/Browserbase, then call static fallback only if browser extraction fails.",
        }
    if mode in {"off", "static", "scrape"}:
        return await _scrape_public_accommodation_site(source, city, country, checkin, checkout, adults, rooms, preferences)
    browser = await _browseruse_get_html(url)
    if browser.get("ok"):
        result = _extract_public_accommodation_from_html(source, browser.get("html", ""), url, preferences)
        result["source"] = f"{source}_browser_use"
        result["browser_engine"] = "browser-use"
        result["browser_logs"] = browser.get("logs", [])
        if result.get("confidence") == "low":
            static_result = await _scrape_public_accommodation_site(source, city, country, checkin, checkout, adults, rooms, preferences)
            result["static_fallback"] = static_result
            if static_result.get("confidence") in {"medium", "high"}:
                return static_result | {"browser_attempt": {"engine": "browser-use", "confidence": "low"}}
        return result
    static_result = await _scrape_public_accommodation_site(source, city, country, checkin, checkout, adults, rooms, preferences)
    static_result["browser_first"] = task_payload
    static_result["browser_attempt"] = browser
    static_result["method"] = "browser-use attempted first; static scrape fallback returned this result"
    return static_result


async def _scrape_public_accommodation_site(source: str, city: str, country: str | None, checkin: str, checkout: str, adults: int, rooms: int, preferences: dict[str, Any]) -> dict[str, Any]:
    url = _build_public_accommodation_url(source, city, country, checkin, checkout, adults, rooms)
    async with _client() as client:
        try:
            r = await client.get(
                url,
                headers={
                    "User-Agent": _env("NOMAD_TRAVEL_BROWSER_UA") or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                },
            )
            if r.status_code >= 400:
                return {
                    "source": f"{source}_static_scrape",
                    "url": str(r.url),
                    "count": 0,
                    "listings": [],
                    "confidence": "low",
                    "error": f"HTTP {r.status_code}",
                    "browser_fallback": _accommodation_browser_fallback(source, str(r.url), preferences),
                }
            result = _extract_public_accommodation_from_html(source, r.text, str(r.url), preferences)
            if result.get("confidence") == "low" and "browser_fallback" not in result:
                result["browser_fallback"] = _accommodation_browser_fallback(source, str(r.url), preferences)
            return result
        except Exception as exc:
            return {
                "source": f"{source}_static_scrape",
                "url": url,
                "count": 0,
                "listings": [],
                "confidence": "low",
                "error": str(exc),
                "browser_fallback": _accommodation_browser_fallback(source, url, preferences),
            }


def _normalize_booking_api_result(result: dict[str, Any], preferences: dict[str, Any]) -> dict[str, Any]:
    listings = []
    for item in result.get("results", []):
        listing = {
            "source": "booking.com_demand_api",
            "name": item.get("name"),
            "property_type": item.get("property_type"),
            "currency": item.get("currency"),
            "nightly_price": item.get("nightly_price"),
            "total_price": item.get("total_price"),
            "rating": item.get("review_score"),
            "review_count": item.get("review_count"),
            "url": item.get("url"),
            "snippets": [],
            "raw_id": item.get("id"),
        }
        listing["preference_score"] = _score_preference_match(listing, preferences)
        listings.append(listing)
    return {
        "source": "booking.com_demand_api",
        "count": len(listings),
        "listings": sorted(listings, key=lambda x: (-x.get("preference_score", 0), x.get("nightly_price") or 10**9)),
        "confidence": "high" if listings else "low",
        "warning": result.get("warning"),
    }


async def _tripadvisor_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    key = _env("TRIPADVISOR_API_KEY")
    if not key:
        raise RuntimeError("Missing TRIPADVISOR_API_KEY")
    async with _client() as client:
        r = await client.get(
            f"{TRIPADVISOR_BASE}{path}",
            params={**params, "key": key},
            headers={"accept": "application/json", "User-Agent": USER_AGENT},
        )
        r.raise_for_status()
        return r.json()


def _build_public_flight_url(source: str, origin_iata: str, destination_iata: str, departure_date: str, return_date: str | None, adults: int, currency: str) -> str:
    source = source.lower().strip()
    if source in {"kayak", "kayak.com"}:
        route = f"{origin_iata.upper()}-{destination_iata.upper()}/{departure_date}"
        if return_date:
            route += f"/{return_date}"
        return f"https://www.kayak.com/flights/{route}/{adults}adults?sort=bestflight_a"
    if source in {"skyscanner", "skyscanner.com"}:
        return f"https://www.skyscanner.com/transport/flights/{origin_iata.lower()}/{destination_iata.lower()}/{departure_date.replace('-', '')}/{return_date.replace('-', '') if return_date else ''}/?adults={adults}&currency={currency.upper()}"
    if source in {"google-flights", "google", "flights.google.com"}:
        q = quote_plus(f"flights {origin_iata.upper()} to {destination_iata.upper()} {departure_date}" + (f" return {return_date}" if return_date else ""))
        return f"https://www.google.com/travel/flights?q={q}"
    return f"https://www.google.com/search?q={quote_plus(source + ' flights ' + origin_iata.upper() + ' to ' + destination_iata.upper() + ' ' + departure_date)}"


def _flight_browser_task(source: str, url: str, origin_iata: str, destination_iata: str, departure_date: str, return_date: str | None, adults: int, currency: str) -> dict[str, Any]:
    task = (
        f"Open {url}. Search/extract visible public flight offers from {source} for "
        f"{origin_iata.upper()} to {destination_iata.upper()} departing {departure_date}"
        f"{(' returning ' + return_date) if return_date else ''}, {adults} adult(s), currency {currency.upper()}. "
        "Capture price, currency, airline, departure/arrival times, duration, stops, booking/source URL, and warnings. "
        "Return JSON only. Do not log in, bypass bot checks, solve CAPTCHAs, or purchase anything."
    )
    return _browser_first_task_payload(
        "flight_search",
        url,
        task,
        {
            "source": "string",
            "offers": [
                {
                    "airline": "string|null",
                    "total": "number|null",
                    "currency": "string|null",
                    "departure": "string|null",
                    "arrival": "string|null",
                    "duration": "string|null",
                    "stops": "integer|null",
                    "url": "string|null",
                    "warnings": ["string"],
                }
            ],
            "warnings": ["string"],
        },
    )


def _extract_public_flights_from_html(source: str, html: str, url: str, currency: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", html)
    title_match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = _strip_html(title_match.group(1) if title_match else None)
    prices = _extract_price_candidates(text)
    snippets: list[str] = []
    for pat in [r'(.{0,80}(?:nonstop|direct|stops?|layover|duration|airline|flight).{0,100})', r'(.{0,80}(?:depart|arrival|round trip|one-way).{0,100})']:
        for raw in re.findall(pat, text, re.I):
            clean = _strip_html(raw)
            if clean and clean not in snippets:
                snippets.append(clean)
            if len(snippets) >= 10:
                break
        if len(snippets) >= 10:
            break
    offers = []
    for price in prices[:10]:
        offers.append({
            "source": source,
            "total": price,
            "currency": _currency_from_text(text, currency),
            "airline": None,
            "departure": None,
            "arrival": None,
            "duration": None,
            "stops": None,
            "url": url,
            "snippets": snippets[:5],
        })
    confidence = "medium" if offers else "low"
    return {
        "source": f"{source}_static_scrape",
        "url": url,
        "title": title,
        "count": len(offers),
        "offers": offers,
        "confidence": confidence,
        "snippets": snippets,
        "warning": f"Best-effort public {source} flight scrape. Browser extraction is preferred for flights.",
    }


async def _browser_first_flight_source(source: str, origin_iata: str, destination_iata: str, departure_date: str, return_date: str | None, adults: int, currency: str, browser_mode: str | None = None) -> dict[str, Any]:
    url = _build_public_flight_url(source, origin_iata, destination_iata, departure_date, return_date, adults, currency)
    task_payload = _flight_browser_task(source, url, origin_iata, destination_iata, departure_date, return_date, adults, currency)
    mode = _browser_mode(browser_mode)
    if mode in {"task", "task-only", "browserbase", "browser-use-cloud"}:
        return {
            "source": f"{source}_browser_first_task",
            "url": url,
            "count": 0,
            "offers": [],
            "confidence": "low",
            "browser_first": task_payload,
            "fallback_next": "static_scrape",
        }
    if mode in {"off", "static", "scrape"}:
        async with _client() as client:
            try:
                r = await client.get(url, headers={"User-Agent": _env("NOMAD_TRAVEL_BROWSER_UA") or "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
                if r.status_code < 400:
                    result = _extract_public_flights_from_html(source, r.text, str(r.url), currency)
                    if result.get("confidence") == "low":
                        result["browser_fallback"] = task_payload
                    return result
                return {"source": f"{source}_static_scrape", "url": str(r.url), "count": 0, "offers": [], "confidence": "low", "error": f"HTTP {r.status_code}", "browser_fallback": task_payload}
            except Exception as exc:
                return {"source": f"{source}_static_scrape", "url": url, "count": 0, "offers": [], "confidence": "low", "error": str(exc), "browser_fallback": task_payload}
    browser = await _browseruse_get_html(url)
    if browser.get("ok"):
        result = _extract_public_flights_from_html(source, browser.get("html", ""), url, currency)
        result["source"] = f"{source}_browser_use"
        result["browser_engine"] = "browser-use"
        result["browser_logs"] = browser.get("logs", [])
        if result.get("confidence") == "low":
            result["browser_first"] = task_payload
        return result
    static_result = await _browser_first_flight_source(source, origin_iata, destination_iata, departure_date, return_date, adults, currency, "static")
    static_result["browser_first"] = task_payload
    static_result["browser_attempt"] = browser
    static_result["method"] = "browser-use attempted first; static scrape fallback returned this result"
    return static_result


@mcp.tool()
async def search_flights(
    origin_iata: str,
    destination_iata: str,
    departure_date: str,
    return_date: str | None = None,
    adults: int = 1,
    currency: str = "USD",
    max_results: int = 10,
    non_stop: bool = False,
    sources: list[str] | None = None,
    browser_mode: str | None = None,
) -> dict[str, Any]:
    """Search flight offers browser-first via public travel sites, then fallback to static scrape/Amadeus API when available."""
    sources = sources or ["google-flights", "kayak", "skyscanner"]
    key = f"flights:{origin_iata}:{destination_iata}:{departure_date}:{return_date}:{adults}:{currency}:{max_results}:{non_stop}:{sources}:{_browser_mode(browser_mode)}"
    cached = _cache_get(key)
    if cached:
        return cached
    browser_results = await asyncio.gather(
        *(
            _browser_first_flight_source(src, origin_iata, destination_iata, departure_date, return_date, adults, currency, browser_mode)
            for src in sources
        ),
        return_exceptions=True,
    )
    source_results: list[dict[str, Any]] = []
    offers: list[dict[str, Any]] = []
    warnings: list[str] = []
    for src, result in zip(sources, browser_results):
        if isinstance(result, Exception):
            url = _build_public_flight_url(src, origin_iata, destination_iata, departure_date, return_date, adults, currency)
            result = {
                "source": f"{src}_browser_first_error",
                "url": url,
                "count": 0,
                "offers": [],
                "confidence": "low",
                "error": str(result),
                "browser_first": _flight_browser_task(src, url, origin_iata, destination_iata, departure_date, return_date, adults, currency),
            }
        source_results.append(result)
        offers.extend(result.get("offers", []))
        if result.get("error"):
            warnings.append(f"{src}: {result.get('error')}")
        if result.get("confidence") == "low" and (result.get("browser_first") or result.get("browser_fallback")):
            warnings.append(f"{src}: browser/static extraction low-confidence; execute browser task payload for latest visible prices")

    offers.sort(key=lambda x: (x.get("total") is None, x.get("total") or 10**12))
    if offers:
        return _cache_set(
            key,
            {
                "source": "browser_first_flight_search",
                "count": len(offers[:max_results]),
                "offers": offers[:max_results],
                "source_results": source_results,
                "warnings": warnings,
                "browser_engine_status": _browser_engine_status(),
                "method": "Browser-use/Browserbase-first public flight search with static scrape fallback.",
            },
        )

    amadeus_error = None
    amadeus_offers: list[dict[str, Any]] = []
    try:
        data = await _amadeus_get(
            "/v2/shopping/flight-offers",
            {
                "originLocationCode": origin_iata.upper(),
                "destinationLocationCode": destination_iata.upper(),
                "departureDate": departure_date,
                "returnDate": return_date,
                "adults": adults,
                "currencyCode": currency.upper(),
                "max": max_results,
                "nonStop": str(non_stop).lower(),
            },
        )
        amadeus_offers = [_normalize_flight_offer(x, data.get("dictionaries", {})) for x in data.get("data", [])]
        amadeus_offers.sort(key=lambda x: (x.get("total") is None, x.get("total") or 10**9))
    except Exception as exc:
        amadeus_error = str(exc)

    return _cache_set(
        key,
        {
            "source": "browser_first_flight_search",
            "count": len(amadeus_offers[:max_results]),
            "offers": amadeus_offers[:max_results],
            "source_results": source_results,
            "warnings": warnings + ([f"amadeus fallback failed: {amadeus_error}"] if amadeus_error else []),
            "amadeus_fallback_used": bool(amadeus_offers),
            "browser_engine_status": _browser_engine_status(),
            "method": "Browser-use/Browserbase-first attempted; static scrape fallback attempted; Amadeus API used only as final structured fallback if configured.",
        },
    )


@mcp.tool()
async def search_accommodations_multi_source(
    city: str,
    checkin: str,
    checkout: str,
    country: str | None = None,
    adults: int = 1,
    rooms: int = 1,
    currency: str = "USD",
    nightly_budget: float | None = None,
    accommodation_types: list[str] | None = None,
    must_haves: list[str] | None = None,
    nice_to_haves: list[str] | None = None,
    avoid: list[str] | None = None,
    private_room: bool = True,
    avoid_hostels: bool = False,
    quality_weight: float = 0.55,
    max_results: int = 10,
    sources: list[str] | None = None,
    booking_city_id: int | None = None,
    booker_country: str = "us",
    browser_mode: str | None = None,
) -> dict[str, Any]:
    """Search accommodations across Booking.com API/public pages, Airbnb, Hostelworld, and other public sites based on user preferences.

    Official provider APIs are used when configured. Public site scrapes are best-effort and return browser-use/Browserbase fallback payloads when blocked or low-confidence.
    """
    sources = sources or ["booking.com", "airbnb", "hostelworld"]
    preferences = {
        "city": city,
        "country": country,
        "checkin": checkin,
        "checkout": checkout,
        "adults": adults,
        "rooms": rooms,
        "currency": currency.upper(),
        "nightly_budget": nightly_budget,
        "accommodation_types": accommodation_types or [],
        "must_haves": must_haves or [],
        "nice_to_haves": nice_to_haves or [],
        "avoid": avoid or [],
        "private_room": private_room,
        "avoid_hostels": avoid_hostels,
        "quality_weight": quality_weight,
        "max_results": max_results,
        "browser_mode": _browser_mode(browser_mode),
    }
    key = f"accom_multi:{city}:{country}:{checkin}:{checkout}:{adults}:{rooms}:{currency}:{nightly_budget}:{accommodation_types}:{must_haves}:{avoid}:{private_room}:{avoid_hostels}:{sources}:{booking_city_id}:{_browser_mode(browser_mode)}"
    cached = _cache_get(key)
    if cached:
        return cached

    async def one_source(source: str) -> dict[str, Any]:
        canonical = source.lower().strip()
        if canonical in {"booking", "booking.com"} and booking_city_id and _env("BOOKING_API_TOKEN") and _env("BOOKING_AFFILIATE_ID"):
            try:
                api_result = await search_booking_accommodations(
                    booking_city_id,
                    checkin,
                    checkout,
                    adults,
                    rooms,
                    booker_country,
                    currency,
                    max_results,
                    nightly_budget,
                    quality_weight,
                )
                return _normalize_booking_api_result(api_result, preferences)
            except Exception as exc:
                public = await _browser_first_accommodation_site("booking.com", city, country, checkin, checkout, adults, rooms, preferences, browser_mode)
                public["api_error"] = str(exc)
                return public
        return await _browser_first_accommodation_site(canonical, city, country, checkin, checkout, adults, rooms, preferences, browser_mode)

    results = await asyncio.gather(*(one_source(src) for src in sources), return_exceptions=True)
    source_results: list[dict[str, Any]] = []
    all_listings: list[dict[str, Any]] = []
    warnings: list[str] = []
    for src, result in zip(sources, results):
        if isinstance(result, Exception):
            url = _build_public_accommodation_url(src, city, country, checkin, checkout, adults, rooms)
            result = {
                "source": f"{src}_static_scrape",
                "url": url,
                "count": 0,
                "listings": [],
                "confidence": "low",
                "error": str(result),
                "browser_fallback": _accommodation_browser_fallback(src, url, preferences),
            }
        source_results.append(result)
        all_listings.extend(result.get("listings", []))
        if result.get("error"):
            warnings.append(f"{src}: {result.get('error')}")
        if result.get("confidence") == "low" and result.get("browser_fallback"):
            warnings.append(f"{src}: static scrape low-confidence; use browser_fallback for better extraction")

    all_listings.sort(key=lambda x: (-x.get("preference_score", 0), x.get("nightly_price") or 10**9))
    response = {
        "source": "multi_source_accommodation_search",
        "preferences": preferences,
        "count": len(all_listings[:max_results]),
        "results": all_listings[:max_results],
        "source_results": source_results,
        "warnings": warnings,
        "method": "Browser-first via browser-use/Browserbase task payloads; static public scrape fallback; Booking.com official API used when credentials and city_id exist unless browser_mode is task-only/static.",
        "browser_engine_status": _browser_engine_status(),
    }
    return _cache_set(key, response)


@mcp.tool()
async def search_booking_accommodations(
    city_id: int,
    checkin: str,
    checkout: str,
    adults: int = 1,
    rooms: int = 1,
    booker_country: str = "us",
    currency: str = "USD",
    max_results: int = 10,
    nightly_budget: float | None = None,
    quality_weight: float = 0.55,
) -> dict[str, Any]:
    """Search live Booking.com accommodation prices. Requires Booking Demand API credentials and city_id."""
    key = f"booking:{city_id}:{checkin}:{checkout}:{adults}:{rooms}:{booker_country}:{currency}:{max_results}:{nightly_budget}:{quality_weight}"
    cached = _cache_get(key)
    if cached:
        return cached
    body = {
        "booker": {"country": booker_country.lower(), "platform": "desktop", "travel_purpose": "leisure"},
        "checkin": checkin,
        "checkout": checkout,
        "city": city_id,
        "currency": currency.upper(),
        "extras": ["extra_charges", "products"],
        "guests": {"number_of_adults": adults, "number_of_rooms": rooms},
        "rows": max_results,
    }
    data = await _booking_post("/accommodations/search", body)
    nights = max(1, (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days)
    results = []
    for item in data.get("data", []):
        price = item.get("price") or item.get("product_price_breakdown") or {}
        total = _safe_float(price.get("book") or price.get("gross_amount") or price.get("all_inclusive_amount"))
        if total is None and isinstance(price.get("book"), dict):
            total = _safe_float(price["book"].get("amount"))
        result = {
            "source": "booking.com_demand_api",
            "id": item.get("id") or item.get("accommodation"),
            "name": item.get("name"),
            "currency": currency.upper(),
            "total_price": total,
            "nightly_price": round(total / nights, 2) if total else None,
            "review_score": item.get("review_score") or item.get("reviewScore"),
            "review_count": item.get("review_count") or item.get("reviewCount"),
            "url": item.get("url"),
            "raw": item,
        }
        result["fit_score"] = _score_accommodation(result, nightly_budget, quality_weight)
        results.append(result)
    results.sort(key=lambda x: (-x["fit_score"], x.get("nightly_price") or 10**9))
    return _cache_set(key, {"source": "booking.com", "count": len(results), "results": results[:max_results]})


@mcp.tool()
async def search_amadeus_hotels_by_city(
    city_iata: str,
    checkin: str,
    checkout: str,
    adults: int = 1,
    currency: str = "USD",
    max_results: int = 10,
    nightly_budget: float | None = None,
    quality_weight: float = 0.55,
) -> dict[str, Any]:
    """Search hotel offers via Amadeus Hotel APIs by city IATA code."""
    key = f"amadeus_hotels:{city_iata}:{checkin}:{checkout}:{adults}:{currency}:{max_results}:{nightly_budget}:{quality_weight}"
    cached = _cache_get(key)
    if cached:
        return cached
    hotels = await _amadeus_get("/v1/reference-data/locations/hotels/by-city", {"cityCode": city_iata.upper(), "radius": 20, "radiusUnit": "KM"})
    hotel_ids = [h.get("hotelId") for h in hotels.get("data", []) if h.get("hotelId")][:50]
    if not hotel_ids:
        return {"source": "amadeus", "count": 0, "results": [], "warning": "No Amadeus hotel ids found for city"}
    offers = await _amadeus_get(
        "/v3/shopping/hotel-offers",
        {
            "hotelIds": ",".join(hotel_ids[:50]),
            "adults": adults,
            "checkInDate": checkin,
            "checkOutDate": checkout,
            "currency": currency.upper(),
            "bestRateOnly": "true",
        },
    )
    nights = max(1, (date.fromisoformat(checkout) - date.fromisoformat(checkin)).days)
    results = []
    for h in offers.get("data", []):
        hotel = h.get("hotel", {})
        cheapest = None
        for offer in h.get("offers", []):
            amount = _price_amount(offer.get("price"))
            if amount is not None and (cheapest is None or amount < cheapest[0]):
                cheapest = (amount, offer)
        if not cheapest:
            continue
        total, offer = cheapest
        result = {
            "source": "amadeus",
            "hotel_id": hotel.get("hotelId"),
            "name": hotel.get("name"),
            "city_code": hotel.get("cityCode"),
            "latitude": hotel.get("latitude"),
            "longitude": hotel.get("longitude"),
            "currency": offer.get("price", {}).get("currency", currency.upper()),
            "total_price": total,
            "nightly_price": round(total / nights, 2),
            "room_type": offer.get("room", {}).get("typeEstimated", {}).get("category"),
            "board_type": offer.get("boardType"),
            "cancellation": offer.get("policies", {}).get("cancellations"),
        }
        result["fit_score"] = _score_accommodation(result, nightly_budget, quality_weight)
        results.append(result)
    results.sort(key=lambda x: (-x["fit_score"], x.get("nightly_price") or 10**9))
    return _cache_set(key, {"source": "amadeus", "count": len(results), "results": results[:max_results]})


@mcp.tool()
async def tripadvisor_public_scrape(query_or_url: str, category: str = "Hotels") -> dict[str, Any]:
    """Scrape public Tripadvisor pages as best-effort fallback. Returns browser-use/Browserbase fallback instructions if static scrape fails."""
    key = f"tripadvisor_scrape:{query_or_url}:{category}"
    cached = _cache_get(key)
    if cached:
        return cached
    result = await _scrape_tripadvisor_public(query_or_url, category)
    return _cache_set(key, result)


@mcp.tool()
async def tripadvisor_location_search(
    query: str,
    category: str = "hotels",
    lat_long: str | None = None,
    radius_km: float | None = None,
    language: str = "en",
) -> dict[str, Any]:
    """Search Tripadvisor locations for hotels, restaurants, attractions, or geos. Requires TRIPADVISOR_API_KEY."""
    params = {"searchQuery": query, "category": category, "language": language}
    if lat_long:
        params["latLong"] = lat_long
    if radius_km:
        params["radius"] = radius_km
        params["radiusUnit"] = "km"
    return await _tripadvisor_get("/location/search", params)


@mcp.tool()
async def tripadvisor_location_details(location_id: str, language: str = "en", currency: str = "USD") -> dict[str, Any]:
    """Fetch Tripadvisor rating, ranking, review count, web URL, address and metadata. Requires TRIPADVISOR_API_KEY."""
    return await _tripadvisor_get(f"/location/{location_id}/details", {"language": language, "currency": currency.upper()})


@mcp.tool()
async def nomad_city_signals(city: str, country: str | None = None) -> dict[str, Any]:
    """Fetch best-effort nomad lifestyle signals from Nomads.com public pages."""
    key = f"nomads:{city}:{country}"
    cached = _cache_get(key)
    if cached:
        return cached
    result = await _scrape_nomads_city(f"{city} {country}" if country else city)
    return _cache_set(key, result)


@mcp.tool()
async def plan_nomad_itinerary(
    origin_iata: str,
    destinations: list[dict[str, Any]],
    start_date: str,
    total_days: int,
    budget_total: float,
    currency: str = "USD",
    adults: int = 1,
    quality_weight: float = 0.55,
    max_flight_results: int = 5,
) -> dict[str, Any]:
    """Build a ranked nomad itinerary from destination preferences, flight prices, accommodation prices, and city signals.

    destinations entries should include: city, iata, stay_days, and optionally booking_city_id.
    If Booking credentials/city ids are missing, the tool falls back to Amadeus hotel search by IATA.
    """
    legs = []
    current_origin = origin_iata.upper()
    cursor = date.fromisoformat(start_date)
    remaining_budget = budget_total
    for dest in destinations:
        city = dest["city"]
        dest_iata = dest.get("iata") or dest.get("city_iata")
        stay_days = int(dest.get("stay_days") or max(1, total_days // max(1, len(destinations))))
        checkin = cursor.isoformat()
        checkout = (cursor + timedelta(days=stay_days)).isoformat()
        flight_task = search_flights(
            current_origin,
            dest_iata,
            checkin,
            None,
            adults,
            currency,
            max_flight_results,
            False,
            dest.get("flight_sources"),
            dest.get("browser_mode"),
        )
        city_task = nomad_city_signals(city, dest.get("country"))
        tripadvisor_task = tripadvisor_public_scrape(f"{city} {dest.get('country', '')} hotels", "Hotels")
        accommodation_task = None
        nightly_budget = (remaining_budget / max(1, total_days)) * 0.55
        accommodation_sources = dest.get("accommodation_sources") or dest.get("sources")
        if accommodation_sources or dest.get("accommodation_preferences"):
            prefs = dest.get("accommodation_preferences") or {}
            accommodation_task = search_accommodations_multi_source(
                city=city,
                country=dest.get("country"),
                checkin=checkin,
                checkout=checkout,
                adults=adults,
                rooms=int(prefs.get("rooms", 1)),
                currency=currency,
                nightly_budget=float(prefs.get("nightly_budget", nightly_budget)) if prefs.get("nightly_budget", nightly_budget) else None,
                accommodation_types=prefs.get("accommodation_types") or prefs.get("types"),
                must_haves=prefs.get("must_haves") or ["clean bathroom", "wifi", "desk", "air conditioning"],
                nice_to_haves=prefs.get("nice_to_haves"),
                avoid=prefs.get("avoid"),
                private_room=bool(prefs.get("private_room", True)),
                avoid_hostels=bool(prefs.get("avoid_hostels", False)),
                quality_weight=quality_weight,
                max_results=10,
                sources=accommodation_sources,
                booking_city_id=dest.get("booking_city_id"),
                booker_country=dest.get("booker_country", "us"),
                browser_mode=prefs.get("browser_mode") or dest.get("browser_mode"),
            )
        elif dest.get("booking_city_id"):
            accommodation_task = search_booking_accommodations(
                int(dest["booking_city_id"]), checkin, checkout, adults, 1, dest.get("booker_country", "us"), currency, 10, nightly_budget, quality_weight
            )
        else:
            accommodation_task = search_amadeus_hotels_by_city(dest_iata, checkin, checkout, adults, currency, 10, nightly_budget, quality_weight)
        flight_res, acc_res, city_res, tripadvisor_res = await asyncio.gather(flight_task, accommodation_task, city_task, tripadvisor_task, return_exceptions=True)
        flight_offers = [] if isinstance(flight_res, Exception) else flight_res.get("offers", [])
        accommodations = [] if isinstance(acc_res, Exception) else acc_res.get("results", [])
        best_flight = flight_offers[0] if flight_offers else None
        best_stay = accommodations[0] if accommodations else None
        flight_cost = (best_flight or {}).get("total") or 0
        stay_cost = (best_stay or {}).get("total_price") or 0
        leg_cost = flight_cost + stay_cost
        remaining_budget -= leg_cost
        legs.append(
            {
                "city": city,
                "iata": dest_iata,
                "checkin": checkin,
                "checkout": checkout,
                "stay_days": stay_days,
                "best_flight": best_flight,
                "best_accommodation": best_stay,
                "nomad_signals": city_res if not isinstance(city_res, Exception) else {"error": str(city_res)},
                "tripadvisor_signals": tripadvisor_res if not isinstance(tripadvisor_res, Exception) else {"error": str(tripadvisor_res)},
                "estimated_leg_cost": round(leg_cost, 2),
                "warnings": [
                    *( [f"flight lookup failed: {flight_res}"] if isinstance(flight_res, Exception) else [] ),
                    *( [f"accommodation lookup failed: {acc_res}"] if isinstance(acc_res, Exception) else [] ),
                    *( [f"tripadvisor scrape failed: {tripadvisor_res}"] if isinstance(tripadvisor_res, Exception) else [] ),
                ],
            }
        )
        current_origin = dest_iata.upper()
        cursor += timedelta(days=stay_days)
    total_estimated = round(sum(x["estimated_leg_cost"] for x in legs), 2)
    return {
        "currency": currency.upper(),
        "budget_total": budget_total,
        "total_estimated": total_estimated,
        "remaining_budget": round(budget_total - total_estimated, 2),
        "budget_status": "within_budget" if total_estimated <= budget_total else "over_budget",
        "legs": legs,
        "method": "Live flight/accommodation APIs where credentials are configured; public city-signal scraping as fallback.",
    }


@mcp.tool()
async def agentmail_latest_verification_code(
    inbox_id: str | None = None,
    subject_hint: str | None = None,
    from_hint: str | None = None,
    minutes: int = 20,
) -> dict[str, Any]:
    """Fetch the latest AgentMail verification code or magic link for browser-based travel-site signup/login. Does not reveal API keys."""
    return await _agentmail_latest_code(inbox_id or _agentmail_default_inbox(), subject_hint, from_hint, minutes)


@mcp.tool()
async def travel_site_signup_guidance(site: str, inbox_id: str | None = None) -> dict[str, Any]:
    """Return browser-use/Browserbase signup guidance that uses AgentMail for email verification instead of asking the user for OTPs."""
    sites = {
        "booking": ("Booking.com", "https://account.booking.com/register"),
        "booking.com": ("Booking.com", "https://account.booking.com/register"),
        "booking-affiliate": ("Booking.com Demand API / Affiliate", "https://developers.booking.com/"),
        "airbnb": ("Airbnb", "https://www.airbnb.com/signup_login"),
        "airbnb.com": ("Airbnb", "https://www.airbnb.com/signup_login"),
        "hostelworld": ("Hostelworld", "https://www.hostelworld.com/"),
        "hostelworld.com": ("Hostelworld", "https://www.hostelworld.com/"),
        "skyscanner": ("Skyscanner", "https://www.skyscanner.com/"),
        "kayak": ("KAYAK", "https://www.kayak.com/"),
        "tripadvisor": ("Tripadvisor", "https://www.tripadvisor.com/RegistrationController"),
        "browser-use": ("browser-use Cloud", "https://cloud.browser-use.com/"),
        "browserbase": ("Browserbase", "https://www.browserbase.com/"),
    }
    name, url = sites.get(site.lower().strip(), (site, f"https://www.google.com/search?q={quote_plus(site + ' signup')}"))
    return _signup_assistance_payload(name, url, inbox_id)


def _usd_to_inr_rate() -> float:
    return float(_env("NOMAD_TRAVEL_USD_INR") or "92.04")


def _network_school_gateway_price_guidance(origin_iata: str, destinations: list[str]) -> dict[str, Any]:
    """Planning-only public snippet estimates for India -> Network School gateways.

    These are intentionally labeled non-live. They are useful when browser/API flight extraction
    returns task payloads but no parsed fares, which is common for public flight sites.
    """
    origin = origin_iata.upper().strip()
    dests = [d.upper().strip() for d in destinations]
    route_ranges: dict[tuple[str, str], tuple[int, int, str]] = {
        ("BLR", "SIN"): (23000, 30000, "Best default from Bengaluru; public snippets showed ~$246/₹23.5K return."),
        ("BLR", "JHB"): (31000, 38000, "Closer to Johor but usually pricier/less clean than flying into Singapore."),
        ("BLR", "KUL"): (23000, 30000, "Cheap backup, but add transfer complexity to Forest City/Johor."),
        ("DEL", "SIN"): (22000, 30000, "Best default from North India; public snippets showed ~$226-$290/₹21.9K+ return."),
        ("DEL", "JHB"): (25000, 49000, "Volatile; only worth it if actually close to SIN pricing and transfer is simpler."),
        ("DEL", "KUL"): (26000, 38000, "Backup gateway; compare total transfer cost against SIN."),
    }
    default_ranges: dict[str, tuple[int, int, str]] = {
        "SIN": (25000, 40000, "Default Network School gateway; usually simplest transfer."),
        "JHB": (30000, 50000, "Closest airport but routing from India may be worse."),
        "KUL": (25000, 45000, "Backup gateway if fares are materially cheaper."),
    }
    routes: list[dict[str, Any]] = []
    for dest in dests:
        low, high, note = route_ranges.get((origin, dest), default_ranges.get(dest, (30000, 55000, "No route-specific snippet estimate; verify live.")))
        routes.append(
            {
                "origin_iata": origin,
                "destination_iata": dest,
                "estimated_return_inr_range": [low, high],
                "estimated_return_usd_range": [round(low / _usd_to_inr_rate(), 2), round(high / _usd_to_inr_rate(), 2)],
                "planning_note": note,
            }
        )
    routes.sort(key=lambda r: (r["estimated_return_inr_range"][0], 0 if r["destination_iata"] == "SIN" else 1))
    cheapest_low = routes[0]["estimated_return_inr_range"][0] if routes else None
    sin = next((r for r in routes if r["destination_iata"] == "SIN"), None)
    recommended = sin if sin and cheapest_low is not None and sin["estimated_return_inr_range"][0] <= cheapest_low + 5000 else (routes[0] if routes else None)
    return {
        "price_type": "public_snippet_planning_estimate_not_live_fare",
        "source_note": "Hard-coded planning ranges from public flight snippets observed during research; always reprice with Google Flights/Skyscanner/AirAsia before booking.",
        "recommended_gateway": recommended["destination_iata"] if recommended else None,
        "recommended_route": recommended,
        "routes": routes,
    }


@mcp.tool()
async def compare_network_school_vs_nomad_base(
    origin_iata: str,
    start_date: str,
    end_date: str,
    nomad_monthly_cost_usd: float = 1500.0,
    ns_monthly_usd: float = 1500.0,
    months: float | None = None,
    adults: int = 1,
    currency: str = "USD",
    flight_destinations: list[str] | None = None,
    browser_mode: str | None = "task-only",
) -> dict[str, Any]:
    """Compare a self-assembled nomad base against Network School for longer stays.

    Network School is modeled as an all-inclusive builder/community stay: membership starts around
    $1500/month with roommates and includes meals, gym, and accommodation. The practical flight
    gateways are Singapore (SIN), Johor Bahru (JHB), and Kuala Lumpur (KUL); SIN is usually the
    default because transfers to the Johor/Forest City area are simpler than JHB routing.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    days = max(1, (end - start).days)
    stay_months = float(months) if months is not None else round(days / 30.0, 2)
    destinations = flight_destinations or ["SIN", "JHB", "KUL"]
    key = f"ns_compare:{origin_iata}:{start_date}:{end_date}:{nomad_monthly_cost_usd}:{ns_monthly_usd}:{stay_months}:{adults}:{currency}:{destinations}:{_browser_mode(browser_mode)}"
    cached = _cache_get(key)
    if cached:
        return cached

    route_results = await asyncio.gather(
        *(
            search_flights(
                origin_iata=origin_iata.upper(),
                destination_iata=dest.upper(),
                departure_date=start_date,
                return_date=end_date,
                adults=adults,
                currency=currency,
                max_results=3,
                non_stop=False,
                sources=["google-flights", "kayak", "skyscanner"],
                browser_mode=browser_mode,
            )
            for dest in destinations
        ),
        return_exceptions=True,
    )

    routes: list[dict[str, Any]] = []
    best_flight_total = None
    for dest, result in zip(destinations, route_results):
        if isinstance(result, Exception):
            routes.append({"destination_iata": dest.upper(), "error": str(result), "offers": [], "confidence": "low"})
            continue
        offers = result.get("offers", [])
        best = offers[0] if offers else None
        total = best.get("total") if best else None
        if isinstance(total, (int, float)) and (best_flight_total is None or total < best_flight_total):
            best_flight_total = float(total)
        routes.append(
            {
                "destination_iata": dest.upper(),
                "recommended_use": "default_gateway" if dest.upper() == "SIN" else ("backup_if_cheaper_or_transfer_needed" if dest.upper() == "JHB" else "backup_if_total_transfer_cost_still_wins"),
                "best_offer": best,
                "offers": offers[:3],
                "count": result.get("count", 0),
                "confidence": "medium" if offers else "low",
                "source_results": result.get("source_results", []),
                "warnings": result.get("warnings", []),
            }
        )

    ns_membership_total = round(ns_monthly_usd * stay_months, 2)
    nomad_base_total = round(nomad_monthly_cost_usd * stay_months, 2)
    ns_total_with_best_flight = round(ns_membership_total + best_flight_total, 2) if best_flight_total is not None else None
    flight_price_guidance = _network_school_gateway_price_guidance(origin_iata, destinations)
    recommended_route = flight_price_guidance.get("recommended_route") or {}
    recommended_inr_range = recommended_route.get("estimated_return_inr_range") or [None, None]
    membership_inr = round(ns_membership_total * _usd_to_inr_rate())
    ns_planning_total_inr_range = [
        membership_inr + recommended_inr_range[0] if recommended_inr_range[0] is not None else None,
        membership_inr + recommended_inr_range[1] if recommended_inr_range[1] is not None else None,
    ]

    if nomad_monthly_cost_usd >= ns_monthly_usd:
        decision = "network_school"
        reason = "Nomad base monthly cost is at or above Network School, while NS bundles meals, gym, accommodation, structure, and builder/community density."
    elif nomad_monthly_cost_usd <= 1300:
        decision = "nomad_base_if_community_is_real"
        reason = "The self-assembled nomad base is meaningfully cheaper; choose it only if community/social density is strong enough."
    else:
        decision = "close_call"
        reason = "Cost gap is small; choose Network School for community/structure, or the nomad base only for destination-specific lifestyle preference."

    response = {
        "source": "network_school_nomad_comparison",
        "origin_iata": origin_iata.upper(),
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "months": stay_months,
        "currency": currency.upper(),
        "network_school": {
            "monthly_usd": ns_monthly_usd,
            "membership_total_usd": ns_membership_total,
            "included": ["roommates/shared accommodation", "meals", "gym", "community", "events/structure"],
            "primary_gateway": "SIN",
            "backup_gateways": ["JHB", "KUL"],
            "public_source": "https://ns.com/ states membership starts at $1500/month with roommates and includes meals, gym, and accommodations.",
        },
        "nomad_base": {
            "monthly_cost_usd": nomad_monthly_cost_usd,
            "base_total_usd": nomad_base_total,
            "thresholds": {
                "under_1300": "self-assembled nomad base can win on cost",
                "1300_to_1500": "close call; community fit decides",
                "over_1500": "Network School usually wins",
            },
        },
        "flight_routes": routes,
        "flight_price_guidance": flight_price_guidance,
        "best_detected_flight_total": best_flight_total,
        "network_school_total_with_best_detected_flight": ns_total_with_best_flight,
        "network_school_total_planning_estimate": {
            "price_type": "membership_plus_public_snippet_flight_estimate_not_live_fare",
            "membership_inr": membership_inr,
            "flight_inr_range": recommended_inr_range,
            "total_inr_range": ns_planning_total_inr_range,
            "usd_inr_rate": _usd_to_inr_rate(),
            "recommended_gateway": flight_price_guidance.get("recommended_gateway"),
        },
        "decision": decision,
        "reason": reason,
        "method": "Compares all-inclusive Network School membership against a self-assembled nomad base and checks SIN/JHB/KUL return flight routes via the existing browser-first flight search. Use browser_mode='auto' for live browser extraction; task-only returns executable browser task payloads.",
    }
    return _cache_set(key, response)


@mcp.tool()
async def provider_status() -> dict[str, Any]:
    """Show which travel data providers are configured, without revealing secrets."""
    return {
        "amadeus": bool(_env("AMADEUS_CLIENT_ID") and _env("AMADEUS_CLIENT_SECRET")),
        "booking_com": bool(_env("BOOKING_API_TOKEN") and _env("BOOKING_AFFILIATE_ID")),
        "tripadvisor": bool(_env("TRIPADVISOR_API_KEY")),
        "tripadvisor_public_scrape": True,
        "nomads_com_scrape": True,
        "browser_fallback_protocol": True,
        "public_accommodation_sources": ["airbnb", "booking.com", "hostelworld", "agoda"],
        "public_flight_sources": ["google-flights", "kayak", "skyscanner"],
        "browser_engine": _browser_engine_status(),
        "agentmail": _agentmail_status(),
        "network_school": {
            "comparison_tool": "compare_network_school_vs_nomad_base",
            "monthly_usd_starting_with_roommates": 1500,
            "included": ["meals", "gym", "accommodation", "community"],
            "practical_gateways": ["SIN", "JHB", "KUL"],
        },
        "cache_ttl_seconds": CACHE_TTL,
        "amadeus_env": _env("AMADEUS_ENV") or "test",
        "booking_env": _env("BOOKING_ENV") or "production",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
