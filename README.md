# Nomad Travel Planner MCP

MCP server that lets an AI travel agent build nomad-style itineraries using live flight prices, accommodation prices, Tripadvisor quality signals, and nomad city/lifestyle signals.

## What it uses

Primary providers/engines:

- Browser-use / Browserbase-style browser automation
  - Primary path for checking public flight/accommodation websites because it sees the same dynamic prices users see.
  - If `browser-use` CLI is installed, `browser_mode=auto` tries it directly.
  - If not installed, tools return browser-first task payloads for browser-use cloud/Browserbase/native browser tools, then include Firecrawl/static/API fallback data when possible.
- Firecrawl SDK/API
  - Preferred scrape layer for public pages before raw static HTTP scraping when `FIRECRAWL_API_KEY` is configured.
  - Used for Tripadvisor public pages, Nomads.com city pages, public accommodation pages, and public flight search pages.
  - The server also exposes `firecrawl_scrape` for direct public URL extraction.
  - Firecrawl MCP server can be installed separately in the host AI, but this MCP works standalone through the Firecrawl Python SDK/API.
- AgentMail
  - Primary email strategy for signup/login verification flows.
  - The agent enters an AgentMail inbox on travel sites and polls AgentMail for magic links/codes instead of asking the user to manually read OTPs.
- Amadeus Self-Service APIs
  - Flight Offers Search: live flight prices across 400+ airlines.
  - Hotel Search / Hotel Offers: live hotel availability and prices.
- Booking.com Demand API
  - Accommodation search, availability, prices, reviews, property details.
  - Requires affiliate access.
- Tripadvisor Content API
  - Location search, ratings, review counts, ranking, address, web URL.
- Tripadvisor public pages
  - Best-effort static scraper for quality/review/ranking snippets when API credentials are missing.
  - If static scraping fails, the tool returns a structured `browser_fallback` payload for browser-use or Browserbase.
- Airbnb / Hostelworld / Agoda / public Booking pages
  - Best-effort public accommodation discovery when official APIs are missing.
  - Results are scored against user preferences and include browser fallback payloads when blocked.
- Nomads.com public pages
  - Best-effort scraper for cost-of-living and internet-speed signals.
  - If static scraping fails, the result includes browser-use/Browserbase fallback instructions.
- Network School / ns.com comparison
  - Compares the all-inclusive $1,500/month NS bundle against a self-assembled nomad base for longer stays.
  - Checks practical flight gateways: Singapore `SIN`, Johor Bahru `JHB`, and Kuala Lumpur `KUL`.

Important: flight and accommodation prices are only as fresh as the configured provider. For a serious product, use official APIs first. Scraping travel sites for pricing is brittle and legally annoying.

## Install

```bash
cd /path/to/nomad-travel-planner-mcp
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

## Environment

Set only the provider credentials you have. The server degrades gracefully.

```bash
export AMADEUS_CLIENT_ID="..."
export AMADEUS_CLIENT_SECRET="..."
export AMADEUS_ENV="test"          # test or production

export BOOKING_API_TOKEN="..."
export BOOKING_AFFILIATE_ID="..."
export BOOKING_ENV="production"    # production or sandbox

export TRIPADVISOR_API_KEY="..."
export FIRECRAWL_API_KEY="..."      # recommended for public-page scraping
export FIRECRAWL_API_BASE="https://api.firecrawl.dev"
export AGENTMAIL_API_KEY="..."      # optional but recommended for signup/login verification
export AGENTMAIL_INBOX_ID="agent_cortex@agentmail.to"
export NOMAD_TRAVEL_BROWSER_MODE="auto"  # auto, task-only, static/off
export NOMAD_TRAVEL_CACHE_TTL_SECONDS="900"
```

Optional browser setup:

```bash
pip install browser-use
browser-use doctor
# or use browser-use cloud / Browserbase from the host AI using returned task payloads
```

Do not put secrets in chat. Use your AI client's secret/env system.

## Hermes config example

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  nomad_travel:
    command: "/path/to/nomad-travel-planner-mcp/.venv/bin/nomad-travel-mcp"
    env:
      AMADEUS_CLIENT_ID: "YOUR_AMADEUS_CLIENT_ID"
      AMADEUS_CLIENT_SECRET: "YOUR_AMADEUS_CLIENT_SECRET"
      AMADEUS_ENV: "production"
      BOOKING_API_TOKEN: "YOUR_BOOKING_TOKEN"
      BOOKING_AFFILIATE_ID: "YOUR_AFFILIATE_ID"
      TRIPADVISOR_API_KEY: "YOUR_TRIPADVISOR_KEY"
      FIRECRAWL_API_KEY: "YOUR_FIRECRAWL_KEY"
      AGENTMAIL_API_KEY: "YOUR_AGENTMAIL_KEY"
      AGENTMAIL_INBOX_ID: "your-inbox@agentmail.to"
      NOMAD_TRAVEL_BROWSER_MODE: "auto"
    timeout: 180
    connect_timeout: 60
```

Restart Hermes after editing config.

## Claude Desktop config example

```json
{
  "mcpServers": {
    "nomad_travel": {
      "command": "/path/to/nomad-travel-planner-mcp/.venv/bin/nomad-travel-mcp",
      "env": {
        "AMADEUS_CLIENT_ID": "YOUR_AMADEUS_CLIENT_ID",
        "AMADEUS_CLIENT_SECRET": "YOUR_AMADEUS_CLIENT_SECRET",
        "AMADEUS_ENV": "production",
        "BOOKING_API_TOKEN": "YOUR_BOOKING_TOKEN",
        "BOOKING_AFFILIATE_ID": "YOUR_AFFILIATE_ID",
        "TRIPADVISOR_API_KEY": "YOUR_TRIPADVISOR_KEY",
        "FIRECRAWL_API_KEY": "YOUR_FIRECRAWL_KEY",
        "AGENTMAIL_API_KEY": "YOUR_AGENTMAIL_KEY",
        "AGENTMAIL_INBOX_ID": "your-inbox@agentmail.to",
        "NOMAD_TRAVEL_BROWSER_MODE": "auto"
      }
    }
  }
}
```

## Tools

- `provider_status`
  - Shows which providers are configured, without leaking secrets.
  - Includes Firecrawl readiness (`firecrawl.configured`, SDK availability, API base).

- `firecrawl_scrape`
  - Inputs: public URL, optional formats, main-content flag, wait time.
  - Uses Firecrawl Python SDK when available, with Firecrawl v2 REST fallback.
  - Returns markdown/html metadata and truncates large content for safe MCP responses.

- `search_flights`
  - Inputs: origin IATA, destination IATA, departure date, optional return date, adults, currency, sources, browser_mode.
  - Primary: browser-use/Browserbase task extraction from public flight sites like Google Flights/KAYAK/Skyscanner.
  - Fallback: static scrape; final structured fallback to Amadeus if configured.

- `search_accommodations_multi_source`
  - Searches Booking.com, Airbnb, Hostelworld, Agoda/public sources based on user preferences.
  - Inputs include city, dates, budget, accommodation types, must-haves, avoid-list, privacy preference, and source list.
  - Uses browser-use/Browserbase first for Airbnb/Booking/Hostelworld/Agoda; falls back to static scrape.
  - Uses Booking.com Demand API when configured and `booking_city_id` is supplied as structured fallback/provider.

- `search_booking_accommodations`
  - Inputs: Booking city id, check-in/out, adults, rooms, country, currency, quality weighting.
  - Provider: Booking.com Demand API.

- `search_amadeus_hotels_by_city`
  - Inputs: city IATA, check-in/out, adults, currency.
  - Provider: Amadeus hotel list + hotel offers.

- `tripadvisor_public_scrape`
  - Best-effort public Tripadvisor scraper for query or direct URL.
  - Returns extracted name/rating/review/ranking/address snippets when static HTML works.
  - Returns `browser_fallback.browser_use_cli`, `browser_use_agent_task`, and `browserbase_task` when Tripadvisor blocks or dynamically renders the page.

- `tripadvisor_location_search`
  - Search Tripadvisor hotels/restaurants/attractions/geos.

- `tripadvisor_location_details`
  - Fetch Tripadvisor rating, ranking, review count, address, URL, etc.

- `nomad_city_signals`
  - Best-effort Nomads.com scrape for cost/month and internet speed candidates.

- `agentmail_latest_verification_code`
  - Polls AgentMail for the latest magic link or numeric verification code for a signup/login flow.
  - Used by browser-use/Browserbase flows so the agent does not ask the user to manually retrieve OTPs.

- `travel_site_signup_guidance`
  - Returns safe browser-use/Browserbase guidance for signup/login setup on Airbnb, Booking, Hostelworld, Tripadvisor, Browserbase, etc.
  - Uses AgentMail as the primary email verification path.
  - Never asks for passwords/cards/API keys in chat; user intervention is only for CAPTCHA, payment, or explicit approval gates.

- `plan_nomad_itinerary`
  - Builds a ranked itinerary using flights, accommodation, and nomad signals.

- `compare_network_school_vs_nomad_base`
  - Compares Network School / ns.com against a self-assembled nomad base for 2–3+ month stays.
  - Models Network School as $1500/month with roommates including meals, gym, and accommodation.
  - Inputs: origin IATA, start/end dates, expected nomad monthly cost, NS monthly cost, optional flight gateway list.
  - Uses existing browser-first flight search for `SIN`, `JHB`, and `KUL` by default.
  - Includes India → gateway planning fare ranges when live/browser extraction only returns task payloads.
  - Labels those fallback ranges as planning estimates, not live fares.
  - Returns a decision rule: nomad base under `$1,300/month` can win on cost; `$1,300–$1,500` is a close call; over `$1,500` usually favors NS.

## Example prompt after installation

Plan a 45-day nomad itinerary from DEL starting 2026-06-15. I want Bangkok, Chiang Mai, Da Nang, and Bali. Budget is 2500 USD excluding food. Prioritize clean stays, fast internet, and low flight cost. Use live flight and accommodation prices, then explain tradeoffs.

Destination objects for `plan_nomad_itinerary` should include:

```json
[
  {"city":"Bangkok", "country":"Thailand", "iata":"BKK", "stay_days":10},
  {"city":"Chiang Mai", "country":"Thailand", "iata":"CNX", "stay_days":12},
  {"city":"Da Nang", "country":"Vietnam", "iata":"DAD", "stay_days":11},
  {"city":"Bali", "country":"Indonesia", "iata":"DPS", "stay_days":12}
]
```

Add `booking_city_id` when you have a Booking.com city id; otherwise the tool uses Amadeus hotel search by IATA.

## Provider notes from research

- Browser-use/Browserbase: preferred runtime path for public flight/accommodation sites because many are dynamic and region/session-dependent.
- Firecrawl: preferred public-page scrape layer before raw static HTTP scraping. The implementation follows Firecrawl v2 `/scrape` docs and Python quickstart from `https://docs.firecrawl.dev/llms.txt`: install `firecrawl-py`, set `FIRECRAWL_API_KEY`, scrape pages into markdown/html, and use Interact/MCP separately for richer browser actions when needed.
- Amadeus Flight Offers Search: official REST API for live flight prices and availability; test and production environments exist. Used as structured fallback when browser/static extraction fails or when user wants API-only.
- Amadeus Hotel Search: official REST API for hotel offers, available rooms, room details, and prices from 150k+ hotels.
- Booking.com Demand API: official affiliate API; accommodation search/check availability endpoints require Bearer auth and `X-Affiliate-Id`.
- Airbnb: no stable broadly available official public API was found; use public search-page fallback or third-party providers only if the user configures them explicitly.
- Hostelworld: treat as public-page fallback unless a partner API is configured externally.
- Tripadvisor Content API: official API for location search/details/photos/reviews. Useful for quality, ranking, and review-count signals, not usually the best source for live room prices.
- Tripadvisor public scrape: fallback only. Tripadvisor commonly returns 403 or dynamic pages; when this happens the MCP tool returns browser-use and Browserbase task specs so the host agent can switch to browser automation instead of hallucinating.
- Nomads.com/Nomad List: useful for nomad lifestyle data, but no stable public official API surfaced in research. Scraping should be treated as best-effort only; fallback browser instructions are returned on scrape failure.

## Development check

```bash
python3 -m py_compile nomad_travel_mcp/server.py
python3 -m pip install -e .
nomad-travel-mcp
```

The last command starts a stdio MCP server and waits for an MCP client.
