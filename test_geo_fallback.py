"""
Deterministic proof of the geo provider fallback chain, per the capstone brief's
instruction to mock providers for this specific proof (since real free-tier APIs
are rate-limited and non-deterministic, as directly observed: ipapi.co returned
a real 429 during manual testing).
"""
from unittest.mock import patch
import geo


def test_provider_a_success():
    with patch("geo._provider_a", return_value={"country": "MockCountryA", "city": "MockCityA"}):
        result = geo.enrich_ip("1.2.3.4")
        assert result == {"country": "MockCountryA", "city": "MockCityA"}
        print("Provider A succeeds -> used directly:", result)


def test_provider_a_fails_provider_b_succeeds():
    with patch("geo._provider_a", return_value=None), \
         patch("geo._provider_b", return_value={"country": "MockCountryB", "city": "MockCityB"}):
        result = geo.enrich_ip("1.2.3.4")
        assert result == {"country": "MockCountryB", "city": "MockCityB"}
        print("Provider A fails, B succeeds -> fallback used:", result)


def test_both_providers_fail():
    with patch("geo._provider_a", return_value=None), \
         patch("geo._provider_b", return_value=None):
        result = geo.enrich_ip("1.2.3.4")
        assert result == {"country": None, "city": None}
        print("Both providers fail -> graceful degradation, no crash:", result)


if __name__ == "__main__":
    test_provider_a_success()
    test_provider_a_fails_provider_b_succeeds()
    test_both_providers_fail()
    print("\nAll fallback chain scenarios passed.")