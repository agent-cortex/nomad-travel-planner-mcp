import asyncio
import os

from nomad_travel_mcp.server import (
    nomad_city_signals,
    provider_status,
    search_accommodations_multi_source,
    search_flights,
    travel_site_signup_guidance,
    firecrawl_scrape,
    agentmail_latest_verification_code,
    compare_network_school_vs_nomad_base,
    tripadvisor_public_scrape,
)


def test_provider_status_shape():
    result = asyncio.run(provider_status())
    assert "amadeus" in result
    assert "booking_com" in result
    assert "tripadvisor" in result
    assert result["tripadvisor_public_scrape"] is True
    assert result["nomads_com_scrape"] is True
    assert result["browser_fallback_protocol"] is True
    assert "airbnb" in result["public_accommodation_sources"]
    assert "hostelworld" in result["public_accommodation_sources"]
    assert "kayak" in result["public_flight_sources"]
    assert "browser_engine" in result
    assert "firecrawl" in result
    assert "configured" in result["firecrawl"]
    assert "network_school" in result
    assert result["network_school"]["comparison_tool"] == "compare_network_school_vs_nomad_base"


def test_nomad_city_signals_no_crash():
    result = asyncio.run(nomad_city_signals("Bangkok", "Thailand"))
    assert result["source"] == "nomads.com_scrape"


def test_tripadvisor_public_scrape_no_crash():
    result = asyncio.run(tripadvisor_public_scrape("Bangkok hotels", "Hotels"))
    assert result["source"] == "tripadvisor_static_scrape"
    assert result["confidence"] in {"low", "medium", "high"}
    if result["confidence"] == "low":
        assert "browser_fallback" in result
        assert "browser_use_cli" in result["browser_fallback"]
        assert "browserbase_task" in result["browser_fallback"]


def test_multi_source_accommodation_browser_task_mode():
    result = asyncio.run(
        search_accommodations_multi_source(
            city="Da Nang",
            country="Vietnam",
            checkin="2026-07-01",
            checkout="2026-07-31",
            currency="USD",
            nightly_budget=35,
            accommodation_types=["studio", "apartment", "hotel"],
            must_haves=["clean bathroom", "wifi", "desk", "air conditioning"],
            avoid=["dorm", "shared bathroom"],
            sources=["airbnb"],
            max_results=3,
            browser_mode="task-only",
        )
    )
    assert result["source"] == "multi_source_accommodation_search"
    assert result["source_results"]
    first = result["source_results"][0]
    assert first["source"] == "airbnb_browser_first_task"
    assert "browser_first" in first
    assert "browserbase_task" in first["browser_first"]


def test_flights_browser_task_mode():
    result = asyncio.run(
        search_flights(
            "DEL",
            "BKK",
            "2026-07-01",
            adults=1,
            currency="USD",
            sources=["kayak"],
            browser_mode="task-only",
        )
    )
    assert result["source"] == "browser_first_flight_search"
    assert result["source_results"]
    assert result["source_results"][0]["source"] == "kayak_browser_first_task"
    assert "browser_first" in result["source_results"][0]


def test_signup_guidance_safe_payload():
    result = asyncio.run(travel_site_signup_guidance("airbnb"))
    assert result["site"] == "Airbnb"
    assert result["primary_email_strategy"] == "agentmail"
    assert result["agentmail"]["poll_tool"] == "agentmail_latest_verification_code"
    assert "browser_use_cli" in result
    assert "Use AgentMail for email verification" in result["hard_rules"][0]


def test_agentmail_latest_verification_code_unconfigured_safe():
    old = os.environ.pop("AGENTMAIL_API_KEY", None)
    try:
        result = asyncio.run(agentmail_latest_verification_code(inbox_id="test@example.com", subject_hint="airbnb", minutes=1))
    finally:
        if old is not None:
            os.environ["AGENTMAIL_API_KEY"] = old
    assert result["configured"] is False
    assert "AGENTMAIL_API_KEY" in result["error"]


def test_firecrawl_scrape_unconfigured_safe():
    old = os.environ.pop("FIRECRAWL_API_KEY", None)
    try:
        result = asyncio.run(firecrawl_scrape("https://example.com"))
    finally:
        if old is not None:
            os.environ["FIRECRAWL_API_KEY"] = old
    assert result["ok"] is False
    assert result["engine"] == "firecrawl"
    assert "FIRECRAWL_API_KEY" in result["error"]


def test_network_school_comparison_task_mode():
    result = asyncio.run(
        compare_network_school_vs_nomad_base(
            origin_iata="DEL",
            start_date="2026-06-15",
            end_date="2026-09-15",
            nomad_monthly_cost_usd=1500,
            flight_destinations=["SIN"],
            browser_mode="task-only",
        )
    )
    assert result["source"] == "network_school_nomad_comparison"
    assert result["decision"] == "network_school"
    assert result["network_school"]["primary_gateway"] == "SIN"
    assert result["flight_routes"][0]["destination_iata"] == "SIN"
    assert result["flight_routes"][0]["source_results"]


def test_network_school_comparison_includes_india_gateway_price_guidance():
    result = asyncio.run(
        compare_network_school_vs_nomad_base(
            origin_iata="BLR",
            start_date="2026-06-15",
            end_date="2026-09-15",
            nomad_monthly_cost_usd=1500,
            flight_destinations=["SIN", "JHB", "KUL"],
            browser_mode="task-only",
        )
    )
    guidance = result["flight_price_guidance"]
    assert guidance["price_type"] == "public_snippet_planning_estimate_not_live_fare"
    assert guidance["recommended_gateway"] == "SIN"
    assert guidance["recommended_route"]["destination_iata"] == "SIN"
    assert guidance["recommended_route"]["estimated_return_inr_range"][1] <= 30000
    assert result["network_school_total_planning_estimate"]["total_inr_range"][0] > 400000
