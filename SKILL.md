---
name: nomad-travel-planner
description: Use when planning digital-nomad itineraries with live flight prices, accommodation costs, stay-quality tradeoffs, and city lifestyle signals from travel APIs and public nomad data sources.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [travel, nomad, itinerary, mcp, flights, hotels, tripadvisor, booking]
    related_skills: [native-mcp]
---

# Nomad Travel Planner

## Overview

Use this skill after installing the `nomad-travel-planner-mcp` server. It turns the AI into a practical nomad travel planner: build itineraries around budget, timeline, destination quality, clean stays, internet speed, transit friction, and live-ish travel prices.

The MCP server provides tools for:

- browser-first flight search via browser-use/Browserbase task payloads for Google Flights/KAYAK/Skyscanner, with Firecrawl/static/API fallback
- Firecrawl SDK/API scraping for public Tripadvisor, Nomads.com, accommodation, and flight pages before raw static HTTP fallback
- accommodation pricing via browser-use/Browserbase on Booking/Airbnb/Hostelworld/Agoda, with Booking.com Demand API or Amadeus hotel offers as structured fallback
- multi-source accommodation discovery via Booking.com, Airbnb, Hostelworld, Agoda/public pages, scored against user preferences
- AgentMail-backed signup/login guidance for travel sites, including magic-link/code polling so the user does not need to manually fetch OTPs
- quality/review metadata via Tripadvisor Content API
- public Tripadvisor scraping for fallback quality/review/ranking snippets
- nomad city signals via best-effort Nomads.com public-page scraping
- browser-use / Browserbase fallback payloads when static scraping gets blocked or returns weak data
- full itinerary assembly with budget scoring
- Network School vs self-assembled nomad-base comparison for longer stays, including SIN/JHB/KUL flight-gateway checks

Opinionated default: browser-use/Browserbase is the primary path for dynamic flight/accommodation sites. Firecrawl is the preferred public-page scrape layer when configured, because it returns cleaner LLM-ready markdown/html than raw static HTTP. If the MCP server cannot directly run a browser, it returns browser-first task payloads for the host agent and includes Firecrawl/static/API fallback data where possible. Official APIs are still better for structured pricing at scale.

## When to Use

Use this when the user asks for:

- a digital nomad itinerary
- multi-city travel planning with budget constraints
- latest flight costs and accommodation costs
- remote-work-friendly places to stay
- destination ranking by cost, internet, safety, weather, quality, or timeline
- “best possible itinerary” with tradeoffs explained

Do not use this for:

- visa/legal immigration advice as final authority
- booking a flight/hotel directly without explicit user approval
- medical/security risk guarantees
- scraping behind login/paywalls or bypassing anti-bot systems

## Required Inputs to Ask For

If missing, make sane defaults, but try to collect:

1. Origin airport IATA code, e.g. `DEL`, `BLR`, `BOM`.
2. Start date and total trip length.
3. Candidate destinations or destination style.
4. Total budget and currency.
5. Stay preference:
   - cheapest clean stay
   - private apartment
   - hotel
   - hostel/private room
   - high-quality bathroom/workdesk/non-negotiables
6. Quality weighting:
   - budget-first: `0.25`
   - balanced: `0.55`
   - comfort-first: `0.75`
7. Work needs:
   - internet speed
   - quiet room
   - desk/chair
   - coworking nearby
8. Pace:
   - slow travel: 2-6 weeks/city
   - medium: 7-14 days/city
   - fast: 3-6 days/city

## MCP Tool Flow

### 1. Check provider readiness

Call:

```text
provider_status
```

Interpretation:

- `amadeus=true`: live flight search and Amadeus hotel search are available.
- `booking_com=true`: Booking.com accommodation search is available.
- `tripadvisor=true`: review/ranking metadata is available.
- `tripadvisor_public_scrape=true`: public Tripadvisor static scraper is available.
- `nomads_com_scrape=true`: best-effort public city signals are available.
- `browser_engine.browser_use_cli=true`: MCP server can directly run browser-use locally.
- `browser_engine.browserbase_configured=true`: host/browser cloud credentials appear configured; browser task payloads can be run by the client.
- `browser_fallback_protocol=true`: failed/low-confidence browser/static extraction returns browser-use and Browserbase task payloads.
- `agentmail.configured=true`: signup/login flows can poll AgentMail for verification codes or magic links.
- `firecrawl.configured=true`: public-page scraping will use Firecrawl SDK/API before brittle raw static HTTP.
- `firecrawl.sdk_available=true`: local Python SDK is installed; otherwise the server can still use Firecrawl v2 REST when `FIRECRAWL_API_KEY` exists.
- `network_school.comparison_tool`: `compare_network_school_vs_nomad_base` is available for checking NS against Chiang Mai/Da Nang/etc. on longer stays.

Never ask the user to paste secrets into chat. Tell them to configure environment variables in their MCP client.

### 2. Convert user plan into destination objects

For `plan_nomad_itinerary`, use objects like:

```json
[
  {"city":"Bangkok", "country":"Thailand", "iata":"BKK", "stay_days":10},
  {"city":"Chiang Mai", "country":"Thailand", "iata":"CNX", "stay_days":12},
  {"city":"Da Nang", "country":"Vietnam", "iata":"DAD", "stay_days":11},
  {"city":"Bali", "country":"Indonesia", "iata":"DPS", "stay_days":12}
]
```

If a Booking.com city id is known, add:

```json
{"booking_city_id": -3414440}
```

If not, omit it. The MCP server falls back to Amadeus hotel search by city IATA.

### 3. Generate the first-pass itinerary

Call:

```text
plan_nomad_itinerary(origin_iata, destinations, start_date, total_days, budget_total, currency, adults, quality_weight)
```

Use:

- `quality_weight=0.25` for strict budget travelers
- `quality_weight=0.55` for balanced nomad plans
- `quality_weight=0.75` when clean/private/comfortable stays matter more than price

### 4. Fill gaps manually with targeted tools

If a leg has missing data:

- flights missing → call `search_flights` with date alternatives ±3 days; prefer `browser_mode="auto"` or `browser_mode="task-only"` if the host agent will execute browser-use/Browserbase. With `FIRECRAWL_API_KEY`, static fallback uses Firecrawl before raw HTTP.
- stays missing → call `search_accommodations_multi_source` when the user cares about Airbnb/Booking/Hostelworld options; call `search_amadeus_hotels_by_city` or `search_booking_accommodations` for official hotel/API-only paths. With Firecrawl configured, public pages are scraped through Firecrawl first.
- quality unclear → first call `tripadvisor_location_search` then `tripadvisor_location_details` if API credentials exist; otherwise call `tripadvisor_public_scrape`, which uses Firecrawl first when configured.
- city lifestyle unclear → call `nomad_city_signals`
- any scraper returns `browser_fallback` → switch to browser-use or Browserbase using the supplied fallback payload; do not invent values

### Accommodation preference search

Use `search_accommodations_multi_source` when the user says things like “find stays”, “compare Airbnb vs Booking”, “hostel/private room”, or gives constraints like clean bathroom, desk, AC, budget/night, no dorms, private room, near beach/coworking.

Suggested defaults for nomad stays:

```json
{
  "accommodation_types": ["studio", "apartment", "hotel"],
  "must_haves": ["clean bathroom", "wifi", "desk", "air conditioning"],
  "nice_to_haves": ["kitchen", "washing machine", "balcony", "coworking nearby"],
  "avoid": ["dorm", "shared bathroom", "party hostel"],
  "private_room": true,
  "avoid_hostels": false,
  "sources": ["booking.com", "airbnb", "hostelworld"],
  "browser_mode": "auto"
}
```

Use `avoid_hostels=true` if the user wants comfort/privacy. Keep Hostelworld enabled only when the user is open to hostels/private hostel rooms.

### Signup / login guidance

If the user wants the agent to sign up or authenticate with travel sites, call `travel_site_signup_guidance(site)`.

Rules:

- Use headed browser-use/profile mode or Browserbase live session.
- Enter the configured AgentMail inbox as the signup/login email.
- When a code or magic link is requested, call `agentmail_latest_verification_code` with the inbox and site hint, then enter the returned code/link in the browser.
- Do not ask the user to manually read/paste OTPs or magic links.
- User intervention is only for CAPTCHA, payment, paid subscription/booking approval, or password-manager/private-account gates.
- Do not collect passwords, OTPs, API keys, or payment details in conversation.
- Do not automate CAPTCHA or anti-bot bypass.
- After authentication, reuse the browser profile/session to query prices directly.

### 5. Compare Network School vs a self-assembled nomad base

When the user is considering **~2–3+ months** and says the monthly nomad-base cost is close to **$1,500**, call:

```text
compare_network_school_vs_nomad_base(origin_iata, start_date, end_date, nomad_monthly_cost_usd, ns_monthly_usd=1500, flight_destinations=["SIN","JHB","KUL"])
```

Use this for Network School / ns.com decisions because the cost bundle is different from normal travel:

- Network School starts around `$1,500/month` with roommates.
- It includes meals, gym, accommodation, community, and structure.
- Practical gateways are `SIN` first, then `JHB`/`KUL` if cheaper or easier.
- The tool now returns `flight_price_guidance` with India → SIN/JHB/KUL planning fare ranges when browser/API extraction has no parsed live fare.
- Treat `flight_price_guidance` as a non-live estimate; use it for planning and reprice before booking.
- If a self-assembled nomad base is `>$1,500/month`, NS usually wins for community/structure.
- If the nomad base is `<$1,300/month`, the nomad base can still win on cost if community is good.

### 6. Optimize

Run at least two variants when the user asks for “best”:

1. cheapest route order
2. quality-balanced route order
3. optional slow-travel version with fewer hops

Pick one winner. Do not dump all raw data unless asked.

## Output Format

Return a compact itinerary:

```text
Best route: DEL → BKK → CNX → DAD → DPS
Budget: $2,500 cap / $2,310 estimated / $190 buffer
Verdict: within budget

1) Bangkok — Jun 15-25 — 10 nights
   Flight: DEL→BKK, $142, 1 stop, best carrier/time note
   Stay: $31/night, $310 total, rating/review note
   Nomad fit: strong internet, good food, easy transit
   Risk: humid/rainy, tourist-heavy

2) Chiang Mai — Jun 25-Jul 7 — 12 nights
   ...

Why this route wins:
- lowest total flight cost among tested orderings
- keeps long stays where accommodation is cheaper
- avoids expensive weekend check-ins where found

Tradeoffs:
- cheapest stay may not satisfy clean bathroom/private desk requirement
- live prices can change; re-check before booking
```

## Budget Rules

Use the user’s stated budget scope exactly.

Common scopes:

- flights + accommodation only
- all-in excluding visa
- all-in including food/local transport

If no scope is stated, assume flights + accommodation only and say so.

Suggested budget split for nomads:

- 30-45% accommodation
- 15-30% flights/intercity transport
- 20-35% food/local transport
- 10-15% buffer

Warn if the generated plan has less than 10% buffer. Nomad plans without buffer are fake precision cosplay.

## Data Source Policy

### Good sources for live prices

- Browser-use/Browserbase on public flight/accommodation sites for current, session-specific visible prices
- Amadeus Flight Offers Search for flights as structured API fallback
- Amadeus Hotel Search / Hotel Offers for hotels as structured API fallback
- Booking.com Demand API for accommodation as structured provider fallback

### Good sources for quality signals

- Tripadvisor Content API ratings, review counts, rankings, details
- Booking.com review score where available
- accommodation amenities from official APIs

### Good sources for nomad lifestyle signals

- Nomads.com public pages, best-effort only
- official city/government/open-data sources when available
- coworking and internet-speed data if exposed by configured providers

### Sources to treat carefully

- scraped Booking/Tripadvisor/Google search pages: brittle and may breach ToS
- cached blog posts: useful for ideas, not current prices
- LLM memory: never use it for current flight/hotel prices

### Browser fallback rule

`search_flights`, `search_accommodations_multi_source`, `nomad_city_signals`, and `tripadvisor_public_scrape` may return `browser_first` or `browser_fallback` objects when direct browser execution is unavailable, blocked, too sparse, or dynamically rendered.

When that happens:

1. Prefer the current AI client's native browser tools if available.
2. If using browser-use CLI, run the returned `browser_use_cli` commands.
3. If using Browserbase, submit the returned `browserbase_task`.
4. Extract only visible/public data. Do not log in, bypass bot checks, solve CAPTCHAs, or scrape paywalled/private content.
5. Mark the result as browser-derived and best-effort.

## API Setup Notes

The MCP server expects env vars, not command-line secrets:

```bash
AMADEUS_CLIENT_ID
AMADEUS_CLIENT_SECRET
AMADEUS_ENV=test|production
BOOKING_API_TOKEN
BOOKING_AFFILIATE_ID
BOOKING_ENV=production|sandbox
TRIPADVISOR_API_KEY
FIRECRAWL_API_KEY
FIRECRAWL_API_BASE=https://api.firecrawl.dev
AGENTMAIL_API_KEY
AGENTMAIL_INBOX_ID=agent_cortex@agentmail.to
NOMAD_TRAVEL_BROWSER_MODE=auto|task-only|static
NOMAD_TRAVEL_CACHE_TTL_SECONDS=900
```

Hermes MCP config shape:

```yaml
mcp_servers:
  nomad_travel:
    command: "/absolute/path/to/nomad-travel-planner-mcp/.venv/bin/nomad-travel-mcp"
    env:
      AMADEUS_CLIENT_ID: "..."
      AMADEUS_CLIENT_SECRET: "..."
      AMADEUS_ENV: "production"
      BOOKING_API_TOKEN: "..."
      BOOKING_AFFILIATE_ID: "..."
      TRIPADVISOR_API_KEY: "..."
      FIRECRAWL_API_KEY: "..."
      AGENTMAIL_API_KEY: "..."
      AGENTMAIL_INBOX_ID: "your-inbox@agentmail.to"
    timeout: 180
    connect_timeout: 60
```

## Common Pitfalls

1. Confusing city names with airport IATA codes.
   - Bangkok can be BKK or DMK; Bali is usually DPS; Da Nang is DAD.

2. Calling plans “latest” when no browser execution happened.
   - If results only contain `browser_first` task payloads, the host agent still needs to execute browser-use/Browserbase before calling them latest.

3. Over-optimizing for cheapest flight.
   - A terrible arrival time can destroy a workday. Penalize overnight arrivals and airport changes unless budget is king.

4. Ignoring accommodation taxes/fees.
   - Prefer all-in totals when APIs expose them. If unclear, add a 10-15% buffer.

5. Treating Nomads.com or Tripadvisor public scraping as stable.
   - Useful, not canonical. Selectors break, 403s happen, dynamic pages happen. Configure Firecrawl for cleaner LLM-ready markdown/html, then use browser fallback payloads when Firecrawl/static scraping still fails.

6. Asking the user to manually fetch email codes.
   - Use AgentMail via `agentmail_latest_verification_code`. Only escalate to the human for CAPTCHA, payment, password, or explicit approval gates.

7. No re-check before booking.
   - Prices change fast. Always tell the user to re-run the tools immediately before paying.

## Verification Checklist

Before final answer:

- [ ] Provider status checked.
- [ ] Budget scope stated.
- [ ] Dates and stay lengths add up to the user’s timeline.
- [ ] Each city has a flight estimate or explicit missing-data warning.
- [ ] Each city has accommodation estimate or explicit missing-data warning.
- [ ] The final plan includes buffer and tradeoffs.
- [ ] “Latest/live” wording is only used for browser-executed or official API-backed results.
- [ ] If a result returns `browser_first`/`browser_fallback`, use browser-use/Browserbase or clearly mark the data as pending browser execution.
- [ ] No secrets are printed.
