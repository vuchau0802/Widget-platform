import requests

# Toggle these to False to deterministically test the fallback/failure paths,
# per the brief's requirement: "mock the geo providers when you prove the fallback."
PROVIDER_A_ENABLED = True
PROVIDER_B_ENABLED = True


def _provider_a(ip: str) -> dict | None:
    """ip-api.com — free, no key, 45 requests/minute."""
    if not PROVIDER_A_ENABLED:
        return None
    try:
        resp = requests.get(f"http://ip-api.com/json/{ip}", timeout=3)
        data = resp.json()
        if data.get("status") == "success":
            return {"country": data.get("country"), "city": data.get("city")}
    except requests.RequestException:
        pass
    return None


def _provider_b(ip: str) -> dict | None:
    """ipapi.co — free tier ~1,000 lookups/day, no key."""
    if not PROVIDER_B_ENABLED:
        return None
    try:
        resp = requests.get(f"https://ipapi.co/{ip}/json/", timeout=3)
        data = resp.json()
        if not data.get("error"):
            return {"country": data.get("country_name"), "city": data.get("city")}
    except requests.RequestException:
        pass
    return None


def enrich_ip(ip: str) -> dict:
    """
    Try provider A, then provider B on failure. If both fail, return empty geo data —
    the submission must still succeed without it. This function must never raise.
    """
    if ip in ("127.0.0.1", "localhost", "testclient"):
        # Local/test IPs never resolve to real geo data — return a clearly-fake
        # value so local testing doesn't silently look like a provider failure.
        return {"country": "Testland", "city": "Localhost"}

    result = _provider_a(ip)
    if result:
        return result

    result = _provider_b(ip)
    if result:
        return result

    return {"country": None, "city": None}