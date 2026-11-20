"""Pure helpers for Google Play regional pricing.

Google Play wants a price for every billable region. Typing ~170 regional
prices by hand is impractical, so the high-level product tools take one price
(e.g. ``"4.99"`` USD), ask Play to convert it with
``monetization.convertRegionPrices``, and use the helpers here to turn that
response into the request bodies the monetization API expects.

Everything in this module is side-effect free so it can be unit tested without
mocking the API.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_NANOS_PER_UNIT = Decimal(1_000_000_000)
_MICROS_PER_UNIT = Decimal(1_000_000)


class PriceError(ValueError):
    """Raised when a price string or a conversion response is unusable."""


def parse_money(amount: str | int | float, currency_code: str = "USD") -> dict[str, Any]:
    """Convert a human price such as ``"4.99"`` into a Play ``Money`` object.

    Args:
        amount: Price in major units (``"4.99"`` means 4 dollars 99 cents).
            Strings are preferred: floats are accepted but go through ``str``
            first so ``0.1`` stays ``0.1``.
        currency_code: ISO 4217 currency code, e.g. ``"USD"`` or ``"EUR"``.

    Returns:
        ``{"currencyCode": ..., "units": "<int as str>", "nanos": <int>}``.

    Raises:
        PriceError: if the amount is not a positive number with at most 9
            decimal places, or the currency code is not 3 letters.
    """
    code = (currency_code or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        raise PriceError(f"currency_code must be a 3-letter ISO 4217 code, got {currency_code!r}")

    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as e:
        raise PriceError(f"price must be a number like '4.99', got {amount!r}") from e

    if not value.is_finite() or value <= 0:
        raise PriceError(f"price must be greater than zero, got {amount!r}")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -9:
        raise PriceError(f"price supports at most 9 decimal places, got {amount!r}")

    units = int(value)  # truncates toward zero; value is positive
    nanos = int((value - units) * _NANOS_PER_UNIT)
    return {"currencyCode": code, "units": str(units), "nanos": nanos}


def money_to_decimal(money: dict[str, Any]) -> Decimal:
    """Return a ``Money`` object's amount as a Decimal (major units)."""
    units = Decimal(str(money.get("units", "0") or "0"))
    nanos = Decimal(int(money.get("nanos", 0) or 0))
    return units + nanos / _NANOS_PER_UNIT


def money_to_micros(money: dict[str, Any]) -> int:
    """Return a ``Money`` object's amount in micros (1/1,000,000 of a unit)."""
    return int(
        (money_to_decimal(money) * _MICROS_PER_UNIT).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )


def format_money(money: dict[str, Any]) -> str:
    """Render a ``Money`` object as ``"4.99 USD"`` for human-readable output."""
    amount = money_to_decimal(money).normalize()
    # normalize() can produce exponent notation (e.g. 1E+1); format plainly.
    text = format(amount, "f")
    return f"{text} {money.get('currencyCode', '')}".strip()


def _converted_items(conversion: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(region_code, price)`` pairs from a convertRegionPrices response."""
    converted = conversion.get("convertedRegionPrices") or {}
    if not converted:
        raise PriceError("convertRegionPrices returned no regional prices")
    items: list[tuple[str, dict[str, Any]]] = []
    for key, entry in sorted(converted.items()):
        region = entry.get("regionCode") or key
        price = entry.get("price")
        if not price:
            continue
        items.append((region, price))
    return items


def _other_regions(conversion: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(usdPrice, eurPrice)`` for "new/other regions" from a conversion."""
    other = conversion.get("convertedOtherRegionsPrice") or {}
    usd = other.get("usdPrice")
    eur = other.get("eurPrice")
    if not usd or not eur:
        raise PriceError("convertRegionPrices returned no other-regions USD/EUR price")
    return usd, eur


def regions_version(conversion: dict[str, Any], default: str) -> str:
    """Return the regions version a conversion was generated at (or ``default``)."""
    version = (conversion.get("regionVersion") or {}).get("version")
    return str(version) if version else default


def subscription_regional_configs(
    conversion: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build base-plan ``regionalConfigs`` and ``otherRegionsConfig`` from a conversion.

    Returns:
        ``(regional_configs, other_regions_config)`` ready to drop into a
        ``BasePlan`` body. Every converted region is made available to new
        subscribers.
    """
    regional = [
        {"regionCode": region, "newSubscriberAvailability": True, "price": price}
        for region, price in _converted_items(conversion)
    ]
    usd, eur = _other_regions(conversion)
    other = {"usdPrice": usd, "eurPrice": eur, "newSubscriberAvailability": True}
    return regional, other


def one_time_regional_configs(
    conversion: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build purchase-option regional configs and ``newRegionsConfig`` from a conversion.

    Returns:
        ``(regional_pricing_and_availability_configs, new_regions_config)``
        ready to drop into a ``OneTimeProductPurchaseOption`` body. Every
        converted region is marked ``AVAILABLE``.
    """
    regional = [
        {"regionCode": region, "price": price, "availability": "AVAILABLE"}
        for region, price in _converted_items(conversion)
    ]
    usd, eur = _other_regions(conversion)
    new_regions = {"usdPrice": usd, "eurPrice": eur, "availability": "AVAILABLE"}
    return regional, new_regions


def summarize_conversion(conversion: dict[str, Any]) -> dict[str, str]:
    """Return ``{region_code: "price CUR"}`` for every converted region."""
    return {region: format_money(price) for region, price in _converted_items(conversion)}
