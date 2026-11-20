"""Tests for the workflow features: regional pricing, priced products,
image batches, listing sync, and the draft-app / review commit fallbacks."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from play_store_mcp import server
from play_store_mcp.client import PlayStoreClient, PlayStoreClientError
from play_store_mcp.models import (
    ImageBatchResult,
    ListingSyncResult,
    ProductSetupResult,
    RegionalPriceConversion,
)
from play_store_mcp.pricing import (
    PriceError,
    format_money,
    money_to_micros,
    one_time_regional_configs,
    parse_money,
    regions_version,
    subscription_regional_configs,
    summarize_conversion,
)

PKG = "com.example.app"


def _http_error(status: int, message: str) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    return HttpError(resp=resp, content=message.encode())


def _usd(units: str, nanos: int = 0) -> dict[str, Any]:
    return {"currencyCode": "USD", "units": units, "nanos": nanos}


CONVERSION: dict[str, Any] = {
    "convertedRegionPrices": {
        "US": {"regionCode": "US", "price": _usd("4", 990_000_000)},
        "DE": {
            "regionCode": "DE",
            "price": {"currencyCode": "EUR", "units": "5", "nanos": 490_000_000},
        },
    },
    "convertedOtherRegionsPrice": {
        "usdPrice": _usd("4", 990_000_000),
        "eurPrice": {"currencyCode": "EUR", "units": "4", "nanos": 590_000_000},
    },
    "regionVersion": {"version": "2025/03"},
}


# =========================================================================
# pricing helpers
# =========================================================================


class TestPricing:
    def test_parse_money_decimal(self) -> None:
        assert parse_money("4.99") == {"currencyCode": "USD", "units": "4", "nanos": 990_000_000}

    def test_parse_money_whole_and_currency_normalized(self) -> None:
        assert parse_money(10, "eur") == {"currencyCode": "EUR", "units": "10", "nanos": 0}

    def test_parse_money_float_is_exact(self) -> None:
        assert parse_money(0.1)["nanos"] == 100_000_000

    @pytest.mark.parametrize("bad", ["abc", "0", "-1", "", "1.0000000001", "nan"])
    def test_parse_money_rejects_bad_amounts(self, bad: str) -> None:
        with pytest.raises(PriceError):
            parse_money(bad)

    @pytest.mark.parametrize("bad", ["US", "DOLLAR", "12$", ""])
    def test_parse_money_rejects_bad_currency(self, bad: str) -> None:
        with pytest.raises(PriceError):
            parse_money("1", bad)

    def test_money_helpers(self) -> None:
        money = _usd("4", 990_000_000)
        assert money_to_micros(money) == 4_990_000
        assert format_money(money) == "4.99 USD"
        assert format_money(_usd("10")) == "10 USD"

    def test_subscription_configs(self) -> None:
        regional, other = subscription_regional_configs(CONVERSION)
        assert [c["regionCode"] for c in regional] == ["DE", "US"]
        assert all(c["newSubscriberAvailability"] for c in regional)
        assert other["usdPrice"]["units"] == "4"
        assert other["newSubscriberAvailability"] is True

    def test_one_time_configs(self) -> None:
        regional, new_regions = one_time_regional_configs(CONVERSION)
        assert {c["availability"] for c in regional} == {"AVAILABLE"}
        assert new_regions["eurPrice"]["currencyCode"] == "EUR"
        assert new_regions["availability"] == "AVAILABLE"

    def test_regions_version_and_summary(self) -> None:
        assert regions_version(CONVERSION, "2022/02") == "2025/03"
        assert regions_version({}, "2022/02") == "2022/02"
        assert summarize_conversion(CONVERSION) == {"DE": "5.49 EUR", "US": "4.99 USD"}

    def test_empty_conversion_rejected(self) -> None:
        with pytest.raises(PriceError):
            one_time_regional_configs({"convertedRegionPrices": {}})
        with pytest.raises(PriceError):
            subscription_regional_configs(
                {"convertedRegionPrices": CONVERSION["convertedRegionPrices"]}
            )


# =========================================================================
# commit fallbacks
# =========================================================================


class TestCommitFallbacks:
    def test_commit_retries_with_changes_not_sent_for_review(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        commit = _mock_service.edits.return_value.commit
        commit.return_value.execute.side_effect = [
            _http_error(
                400,
                "Changes cannot be sent for review automatically. Please set the query "
                "parameter changesNotSentForReview to true.",
            ),
            {},
        ]
        assert client._commit_edit(PKG, "edit-1", allow_not_sent_for_review=True) is False
        assert commit.call_args_list[-1].kwargs["changesNotSentForReview"] is True

    def test_commit_does_not_retry_unless_opted_in(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        """Callers that can't report 'saved but not sent' must still see the error."""
        commit = _mock_service.edits.return_value.commit
        commit.return_value.execute.side_effect = _http_error(
            400, "Please set the query parameter changesNotSentForReview to true."
        )
        with pytest.raises(HttpError):
            client._commit_edit(PKG, "edit-1")
        assert commit.call_count == 1

    def test_halt_release_reports_failure_not_silent_save(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        edits = _mock_service.edits.return_value
        edits.insert.return_value.execute.return_value = {"id": "edit-1"}
        edits.tracks.return_value.get.return_value.execute.return_value = {
            "track": "production",
            "releases": [{"versionCodes": ["5"], "status": "inProgress", "userFraction": 0.1}],
        }
        edits.tracks.return_value.update.return_value.execute.return_value = {}
        edits.commit.return_value.execute.side_effect = _http_error(
            400, "Please set the query parameter changesNotSentForReview to true."
        )
        result = client.halt_release(PKG, "production", 5)
        assert result.success is False
        assert edits.commit.call_count == 1

    def test_commit_reraises_other_errors(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        commit = _mock_service.edits.return_value.commit
        commit.return_value.execute.side_effect = _http_error(403, "Forbidden")
        with pytest.raises(HttpError):
            client._commit_edit(PKG, "edit-1")

    def test_commit_success_returns_true(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        _mock_service.edits.return_value.commit.return_value.execute.return_value = {}
        assert client._commit_edit(PKG, "edit-1") is True

    @pytest.mark.parametrize(
        ("env", "expected"),
        [("error", "ERROR_IF_IN_REVIEW"), ("cancel", "CANCEL_IN_REVIEW_AND_SUBMIT")],
    )
    def test_in_review_behavior_env(
        self,
        client: PlayStoreClient,
        _mock_service: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        env: str,
        expected: str,
    ) -> None:
        monkeypatch.setenv("PLAY_STORE_MCP_IN_REVIEW_BEHAVIOR", env)
        commit = _mock_service.edits.return_value.commit
        commit.return_value.execute.return_value = {}
        client._commit_edit(PKG, "edit-1")
        assert commit.call_args.kwargs["changesInReviewBehavior"] == expected

    def test_in_review_behavior_accepts_api_value(
        self, client: PlayStoreClient, _mock_service: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PLAY_STORE_MCP_IN_REVIEW_BEHAVIOR", "error_if_in_review")
        commit = _mock_service.edits.return_value.commit
        commit.return_value.execute.return_value = {}
        client._commit_edit(PKG, "edit-1")
        assert commit.call_args.kwargs["changesInReviewBehavior"] == "ERROR_IF_IN_REVIEW"

    def test_in_review_behavior_unknown_value_is_ignored(
        self, client: PlayStoreClient, _mock_service: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PLAY_STORE_MCP_IN_REVIEW_BEHAVIOR", "refuse")
        commit = _mock_service.edits.return_value.commit
        commit.return_value.execute.return_value = {}
        client._commit_edit(PKG, "edit-1")
        assert "changesInReviewBehavior" not in commit.call_args.kwargs

    def test_in_review_behavior_unset_sends_nothing(
        self, client: PlayStoreClient, _mock_service: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PLAY_STORE_MCP_IN_REVIEW_BEHAVIOR", raising=False)
        commit = _mock_service.edits.return_value.commit
        commit.return_value.execute.return_value = {}
        client._commit_edit(PKG, "edit-1")
        assert "changesInReviewBehavior" not in commit.call_args.kwargs


class TestDraftAppFallback:
    @pytest.fixture
    def edits(self, _mock_service: MagicMock) -> MagicMock:
        edits = _mock_service.edits.return_value
        edits.insert.return_value.execute.return_value = {"id": "edit-1"}
        edits.bundles.return_value.upload.return_value.execute.return_value = {"versionCode": 7}
        edits.commit.return_value.execute.return_value = {}
        return edits

    def test_deploy_falls_back_to_draft(
        self, client: PlayStoreClient, edits: MagicMock, tmp_path: Any
    ) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        edits.tracks.return_value.update.return_value.execute.side_effect = [
            _http_error(400, "Only releases with status draft may be created on draft app."),
            {},
        ]
        result = client.deploy_app(PKG, "internal", str(aab), rollout_percentage=20.0)

        assert result.success is True
        assert "draft" in result.message
        bodies = [c.kwargs["body"] for c in edits.tracks.return_value.update.call_args_list]
        assert bodies[0]["releases"][0]["status"] == "inProgress"
        assert bodies[-1]["releases"][0]["status"] == "draft"
        assert "userFraction" not in bodies[-1]["releases"][0]

    def test_draft_error_on_commit_also_recovers(
        self, client: PlayStoreClient, edits: MagicMock, tmp_path: Any
    ) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        edits.tracks.return_value.update.return_value.execute.return_value = {}
        edits.commit.return_value.execute.side_effect = [
            _http_error(400, "Only releases with status draft may be created on draft app."),
            {},
        ]
        result = client.deploy_app(PKG, "internal", str(aab))
        assert result.success is True
        last = edits.tracks.return_value.update.call_args.kwargs["body"]["releases"][0]
        assert last["status"] == "draft"

    def test_other_errors_still_fail(
        self, client: PlayStoreClient, edits: MagicMock, tmp_path: Any
    ) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        edits.tracks.return_value.update.return_value.execute.side_effect = _http_error(
            400, "Version code 7 has already been used."
        )
        result = client.deploy_app(PKG, "internal", str(aab))
        assert result.success is False
        assert edits.tracks.return_value.update.call_count == 1
        edits.delete.assert_called()

    @pytest.mark.parametrize(
        ("status", "rollout", "fragment"),
        [
            ("completed", 5.0, "100% of users"),
            ("inProgress", 100.0, "below 100"),
            ("halted", 100.0, "must be one of"),
            ("live", 100.0, "must be one of"),
        ],
    )
    def test_conflicting_release_status_rejected(
        self,
        client: PlayStoreClient,
        edits: MagicMock,
        tmp_path: Any,
        status: str,
        rollout: float,
        fragment: str,
    ) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        result = client.deploy_app(
            PKG, "production", str(aab), rollout_percentage=rollout, release_status=status
        )
        assert result.success is False
        assert fragment in result.message
        edits.insert.assert_not_called()

    def test_in_progress_with_partial_rollout(
        self, client: PlayStoreClient, edits: MagicMock, tmp_path: Any
    ) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        edits.tracks.return_value.update.return_value.execute.return_value = {}
        result = client.deploy_app(
            PKG, "production", str(aab), rollout_percentage=10.0, release_status="inProgress"
        )
        assert result.success is True
        release = edits.tracks.return_value.update.call_args.kwargs["body"]["releases"][0]
        assert release["userFraction"] == pytest.approx(0.1)

    def test_explicit_draft_status(
        self, client: PlayStoreClient, edits: MagicMock, tmp_path: Any
    ) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        edits.tracks.return_value.update.return_value.execute.return_value = {}
        result = client.deploy_app(
            PKG, "production", str(aab), rollout_percentage=10.0, release_status="draft"
        )
        assert result.success is True
        release = edits.tracks.return_value.update.call_args.kwargs["body"]["releases"][0]
        assert release == {"versionCodes": ["7"], "status": "draft"}


# =========================================================================
# regional pricing & priced products
# =========================================================================


@pytest.fixture
def monetization(_mock_service: MagicMock) -> MagicMock:
    mon = _mock_service.monetization.return_value
    mon.convertRegionPrices.return_value.execute.return_value = CONVERSION
    return mon


class TestConvertRegionPrices:
    def test_convert(self, client: PlayStoreClient, monetization: MagicMock) -> None:
        result = client.convert_region_prices(PKG, "4.99")
        assert isinstance(result, RegionalPriceConversion)
        assert result.base_price == "4.99 USD"
        assert result.regions_version == "2025/03"
        assert result.region_count == 2
        assert result.regional_prices["DE"] == "5.49 EUR"
        assert result.other_regions_eur == "4.59 EUR"
        body = monetization.convertRegionPrices.call_args.kwargs["body"]
        assert body == {"price": {"currencyCode": "USD", "units": "4", "nanos": 990_000_000}}

    def test_bad_price(self, client: PlayStoreClient, monetization: MagicMock) -> None:
        with pytest.raises(PlayStoreClientError):
            client.convert_region_prices(PKG, "free")
        monetization.convertRegionPrices.assert_not_called()

    def test_api_error(self, client: PlayStoreClient, monetization: MagicMock) -> None:
        monetization.convertRegionPrices.return_value.execute.side_effect = _http_error(
            403, "Forbidden"
        )
        with pytest.raises(PlayStoreClientError, match="convert regional prices"):
            client.convert_region_prices(PKG, "1")


class TestOneTimeProductWithPrice:
    @pytest.fixture
    def otp(self, monetization: MagicMock) -> MagicMock:
        otp = monetization.onetimeproducts.return_value
        otp.get.return_value.execute.side_effect = _http_error(404, "Not found")
        otp.patch.return_value.execute.return_value = {"productId": "coins_100"}
        otp.purchaseOptions.return_value.batchUpdateStates.return_value.execute.return_value = {}
        return otp

    def test_creates_and_activates(self, client: PlayStoreClient, otp: MagicMock) -> None:
        result = client.create_one_time_product_with_price(
            PKG,
            "coins_100",
            "100 Coins",
            "A pile of coins",
            "4.99",
            extra_listings={"de-DE": {"title": "100 Münzen", "description": "Münzen"}},
        )
        assert isinstance(result, ProductSetupResult)
        assert result.success is True, result.message
        assert result.activated is True
        assert result.region_count == 2
        assert result.regions_version == "2025/03"

        kwargs = otp.patch.call_args.kwargs
        assert kwargs["allowMissing"] is True
        assert kwargs["regionsVersion_version"] == "2025/03"
        body = kwargs["body"]
        assert [lst["languageCode"] for lst in body["listings"]] == ["en-US", "de-DE"]
        option = body["purchaseOptions"][0]
        assert option["purchaseOptionId"] == "default"
        assert option["buyOption"]["legacyCompatible"] is True
        assert len(option["regionalPricingAndAvailabilityConfigs"]) == 2
        assert option["newRegionsConfig"]["availability"] == "AVAILABLE"

        states = otp.purchaseOptions.return_value.batchUpdateStates.call_args.kwargs["body"]
        assert states["requests"][0]["activatePurchaseOptionRequest"]["purchaseOptionId"] == (
            "default"
        )

    def test_existing_product_refused_without_overwrite(
        self, client: PlayStoreClient, otp: MagicMock
    ) -> None:
        otp.get.return_value.execute.side_effect = None
        otp.get.return_value.execute.return_value = {"productId": "coins_100"}
        result = client.create_one_time_product_with_price(PKG, "coins_100", "t", "d", "1")
        assert result.success is False
        assert result.error == "AlreadyExists"
        otp.patch.assert_not_called()

    def test_overwrite_skips_existence_check(self, client: PlayStoreClient, otp: MagicMock) -> None:
        result = client.create_one_time_product_with_price(
            PKG, "coins_100", "t", "d", "1", overwrite=True, activate=False
        )
        assert result.success is True
        assert result.activated is False
        otp.get.assert_not_called()
        otp.purchaseOptions.return_value.batchUpdateStates.assert_not_called()

    def test_activation_failure_is_partial_success(
        self, client: PlayStoreClient, otp: MagicMock
    ) -> None:
        otp.purchaseOptions.return_value.batchUpdateStates.return_value.execute.side_effect = (
            _http_error(400, "Missing tax category")
        )
        result = client.create_one_time_product_with_price(PKG, "coins_100", "t", "d", "1")
        assert result.success is True
        assert result.activated is False
        assert "Missing tax category" in result.warnings[0]
        assert "warning" in result.message

    @pytest.mark.parametrize("bad_id", ["Coins", "_coins", "coins-100", ""])
    def test_invalid_product_id(self, client: PlayStoreClient, otp: MagicMock, bad_id: str) -> None:
        result = client.create_one_time_product_with_price(PKG, bad_id, "t", "d", "1")
        assert result.success is False
        otp.patch.assert_not_called()

    @pytest.mark.usefixtures("otp")
    def test_batch_converts_each_price_once(
        self, client: PlayStoreClient, monetization: MagicMock
    ) -> None:
        result = client.batch_create_one_time_products_with_price(
            PKG,
            [
                {"product_id": "a", "title": "A", "description": "a", "price": "0.99"},
                {"product_id": "b", "title": "B", "description": "b", "price": "0.99"},
                {"product_id": "c", "title": "C"},
            ],
        )
        assert result.successful_count == 2
        assert result.failed_count == 1
        assert result.success is False
        assert "missing" in result.results[2].message
        assert monetization.convertRegionPrices.call_count == 1


class TestSubscriptionWithPrice:
    @pytest.fixture
    def subs(self, monetization: MagicMock) -> MagicMock:
        subs = monetization.subscriptions.return_value
        subs.create.return_value.execute.return_value = {"productId": "premium"}
        subs.basePlans.return_value.activate.return_value.execute.return_value = {
            "productId": "premium"
        }
        return subs

    def test_creates_and_activates(self, client: PlayStoreClient, subs: MagicMock) -> None:
        result = client.create_subscription_with_price(
            PKG,
            "premium",
            "Premium",
            "4.99",
            billing_period="p1y",
            base_plan_id="yearly",
            benefits=["No ads", "Cloud sync"],
            grace_period="P7D",
        )
        assert result.success is True, result.message
        assert result.option_id == "yearly"
        kwargs = subs.create.call_args.kwargs
        assert kwargs["regionsVersion_version"] == "2025/03"
        plan = kwargs["body"]["basePlans"][0]
        assert plan["autoRenewingBasePlanType"] == {
            "billingPeriodDuration": "P1Y",
            "legacyCompatible": True,
            "gracePeriodDuration": "P7D",
        }
        assert len(plan["regionalConfigs"]) == 2
        assert kwargs["body"]["listings"][0]["benefits"] == ["No ads", "Cloud sync"]
        subs.basePlans.return_value.activate.assert_called_once()

    def test_activation_failure_is_partial_success(
        self, client: PlayStoreClient, subs: MagicMock
    ) -> None:
        subs.basePlans.return_value.activate.return_value.execute.side_effect = _http_error(
            400, "Base plan incomplete"
        )
        result = client.create_subscription_with_price(PKG, "premium", "Premium", "4.99")
        assert result.success is True
        assert result.activated is False
        assert "Base plan incomplete" in result.warnings[0]

    def test_with_one_month_free_trial(self, client: PlayStoreClient, subs: MagicMock) -> None:
        offers = subs.basePlans.return_value.offers.return_value
        offers.create.return_value.execute.return_value = {}
        offers.activate.return_value.execute.return_value = {}

        result = client.create_subscription_with_price(
            PKG, "premium", "Premium", "4.99", free_trial="p1m"
        )

        assert result.success is True, result.message
        assert result.free_trial_offer_id == "free-trial"
        assert result.warnings == []
        assert "P1M free trial" in result.message
        kwargs = offers.create.call_args.kwargs
        assert kwargs["basePlanId"] == "monthly"
        assert kwargs["regionsVersion_version"] == "2025/03"
        body = kwargs["body"]
        phase = body["phases"][0]
        assert phase["duration"] == "P1M"
        assert phase["recurrenceCount"] == 1
        assert {c["regionCode"] for c in phase["regionalConfigs"]} == {"DE", "US"}
        assert all(c["free"] == {} for c in phase["regionalConfigs"])
        assert phase["otherRegionsConfig"] == {"free": {}}
        assert {c["regionCode"] for c in body["regionalConfigs"]} == {"DE", "US"}
        assert body["targeting"] == {"acquisitionRule": {"scope": {"thisSubscription": {}}}}
        offers.activate.assert_called_once()

    def test_trial_failure_keeps_subscription(
        self, client: PlayStoreClient, subs: MagicMock
    ) -> None:
        offers = subs.basePlans.return_value.offers.return_value
        offers.create.return_value.execute.side_effect = _http_error(400, "Offer rejected")
        result = client.create_subscription_with_price(
            PKG, "premium", "Premium", "4.99", free_trial="P1M"
        )
        assert result.success is True
        assert result.free_trial_offer_id is None
        assert any("create_free_trial_offer" in w for w in result.warnings)

    def test_trial_not_activated_when_base_plan_inactive(
        self, client: PlayStoreClient, subs: MagicMock
    ) -> None:
        offers = subs.basePlans.return_value.offers.return_value
        offers.create.return_value.execute.return_value = {}
        result = client.create_subscription_with_price(
            PKG, "premium", "Premium", "4.99", free_trial="P7D", activate=False
        )
        assert result.success is True
        assert result.free_trial_offer_id == "free-trial"
        offers.activate.assert_not_called()

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({"free_trial": "1 month"}, "ISO 8601"),
            ({"free_trial": "P1M", "trial_eligibility": "everyone"}, "eligibility"),
            ({"base_plan_id": "Monthly"}, "base_plan_id"),
            ({"benefits": ["1", "2", "3", "4", "5"]}, "benefits"),
        ],
    )
    def test_validation(
        self, client: PlayStoreClient, subs: MagicMock, kwargs: dict[str, Any], fragment: str
    ) -> None:
        result = client.create_subscription_with_price(PKG, "premium", "P", "1", **kwargs)
        assert result.success is False
        assert fragment in result.message
        subs.create.assert_not_called()


class TestFreeTrialOffer:
    @pytest.fixture
    def subs(self, monetization: MagicMock) -> MagicMock:
        subs = monetization.subscriptions.return_value
        subs.get.return_value.execute.return_value = {
            "productId": "premium",
            "basePlans": [
                {
                    "basePlanId": "monthly",
                    "regionalConfigs": [{"regionCode": "US"}, {"regionCode": "FR"}],
                }
            ],
        }
        offers = subs.basePlans.return_value.offers.return_value
        offers.create.return_value.execute.return_value = {}
        offers.activate.return_value.execute.return_value = {}
        return subs

    def test_adds_trial_to_existing_plan(
        self, client: PlayStoreClient, subs: MagicMock, monetization: MagicMock
    ) -> None:
        result = client.create_free_trial_offer(
            PKG, "premium", "monthly", eligibility="new_customers_any_subscription"
        )
        assert result.success is True, result.message
        assert result.region_count == 2
        assert result.activated is True
        body = subs.basePlans.return_value.offers.return_value.create.call_args.kwargs["body"]
        assert body["phases"][0]["duration"] == "P1M"
        assert body["targeting"]["acquisitionRule"]["scope"] == {"anySubscriptionInApp": {}}
        # regions version looked up from Play when not given
        assert monetization.convertRegionPrices.call_count == 1
        kwargs = subs.basePlans.return_value.offers.return_value.create.call_args.kwargs
        assert kwargs["regionsVersion_version"] == "2025/03"

    def test_developer_determined_has_no_targeting(
        self, client: PlayStoreClient, subs: MagicMock
    ) -> None:
        result = client.create_free_trial_offer(
            PKG, "premium", "monthly", "P7D", eligibility="developer_determined"
        )
        assert result.success is True
        body = subs.basePlans.return_value.offers.return_value.create.call_args.kwargs["body"]
        assert "targeting" not in body

    def test_unknown_base_plan(self, client: PlayStoreClient, subs: MagicMock) -> None:
        result = client.create_free_trial_offer(PKG, "premium", "yearly")
        assert result.success is False
        assert "yearly not found" in result.message
        subs.basePlans.return_value.offers.return_value.create.assert_not_called()

    def test_activation_failure_is_partial_success(
        self, client: PlayStoreClient, subs: MagicMock
    ) -> None:
        offers = subs.basePlans.return_value.offers.return_value
        offers.activate.return_value.execute.side_effect = _http_error(400, "Plan inactive")
        result = client.create_free_trial_offer(PKG, "premium", "monthly")
        assert result.success is True
        assert result.activated is False
        assert "Plan inactive" in result.warnings[0]


# =========================================================================
# images
# =========================================================================


def _png(path: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(path)


@pytest.fixture
def image_edits(_mock_service: MagicMock) -> MagicMock:
    edits = _mock_service.edits.return_value
    edits.insert.return_value.execute.return_value = {"id": "edit-1"}
    edits.images.return_value.deleteall.return_value.execute.return_value = {
        "deleted": [{"id": "old1"}, {"id": "old2"}]
    }
    edits.images.return_value.upload.return_value.execute.return_value = {
        "image": {"id": "new", "url": "https://img"}
    }
    edits.listings.return_value.get.return_value.execute.return_value = {
        "title": "Old title",
        "shortDescription": "Old short",
        "fullDescription": "Old full",
    }
    edits.listings.return_value.update.return_value.execute.return_value = {}
    edits.commit.return_value.execute.return_value = {}
    return edits


class TestReplaceImages:
    def test_replace_in_one_commit(
        self, client: PlayStoreClient, image_edits: MagicMock, tmp_path: Any
    ) -> None:
        paths = [_png(tmp_path / f"{i}.png") for i in range(3)]
        result = client.replace_images(PKG, "en-US", "phoneScreenshots", paths)
        assert isinstance(result, ImageBatchResult)
        assert result.success is True, result.message
        assert result.deleted_count == 2
        assert len(result.uploaded) == 3
        assert result.sent_for_review is True
        assert image_edits.images.return_value.upload.call_count == 3
        assert image_edits.commit.call_count == 1
        assert image_edits.insert.call_count == 1

    def test_append_mode_skips_delete(
        self, client: PlayStoreClient, image_edits: MagicMock, tmp_path: Any
    ) -> None:
        result = client.replace_images(
            PKG, "en-US", "icon", [_png(tmp_path / "icon.png")], replace_existing=False
        )
        assert result.success is True
        image_edits.images.return_value.deleteall.assert_not_called()

    @pytest.mark.parametrize(
        ("image_type", "count", "fragment"),
        [
            ("phoneScreenshots", 9, "allows 8"),
            ("icon", 2, "allows 1"),
            ("banner", 1, "Unknown image_type"),
        ],
    )
    def test_validation_blocks_api(
        self,
        client: PlayStoreClient,
        image_edits: MagicMock,
        tmp_path: Any,
        image_type: str,
        count: int,
        fragment: str,
    ) -> None:
        paths = [_png(tmp_path / f"{i}.png") for i in range(count)]
        result = client.replace_images(PKG, "en-US", image_type, paths)
        assert result.success is False
        assert fragment in result.message
        image_edits.insert.assert_not_called()

    @pytest.mark.usefixtures("image_edits")
    def test_missing_file_and_bad_extension(self, client: PlayStoreClient, tmp_path: Any) -> None:
        gif = tmp_path / "a.gif"
        gif.write_bytes(b"GIF")
        result = client.replace_images(
            PKG, "en-US", "phoneScreenshots", [str(tmp_path / "missing.png"), str(gif)]
        )
        assert result.success is False
        assert "file not found" in result.message
        assert ".png/.jpg/.jpeg" in result.message

    def test_upload_failure_discards_edit(
        self, client: PlayStoreClient, image_edits: MagicMock, tmp_path: Any
    ) -> None:
        image_edits.images.return_value.upload.return_value.execute.side_effect = _http_error(
            400, "Image too large"
        )
        result = client.replace_images(PKG, "en-US", "phoneScreenshots", [_png(tmp_path / "1.png")])
        assert result.success is False
        image_edits.commit.assert_not_called()
        image_edits.delete.assert_called()


# =========================================================================
# listing sync
# =========================================================================


@pytest.fixture
def metadata(tmp_path: Any) -> Any:
    root = tmp_path / "metadata"
    en = root / "en-US"
    en.mkdir(parents=True)
    (en / "title.txt").write_text("My App\n", encoding="utf-8")
    (en / "short_description.txt").write_text("Short and sweet", encoding="utf-8")
    _png(en / "images" / "icon.png")
    for name in ["10.png", "2.png", "1.png"]:
        _png(en / "images" / "phoneScreenshots" / name)
    de = root / "de-DE"
    de.mkdir()
    (de / "full_description.txt").write_text("Lange Beschreibung", encoding="utf-8")
    (root / "README.md").write_text("not a language", encoding="utf-8")
    (root / "screenshots").mkdir()  # not a language folder
    return root


class TestListingSync:
    def test_dry_run_touches_nothing(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        result = client.sync_listing_from_directory(PKG, str(metadata), dry_run=True)
        assert isinstance(result, ListingSyncResult)
        assert result.success is True, result.message
        assert result.dry_run is True
        langs = {lang.language: lang for lang in result.languages}
        assert set(langs) == {"de-DE", "en-US"}
        assert langs["en-US"].text_fields == ["shortDescription", "title"]
        assert langs["en-US"].images == {"icon": 1, "phoneScreenshots": 3}
        image_edits.insert.assert_not_called()

    def test_sync_applies_everything_in_one_commit(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        result = client.sync_listing_from_directory(PKG, str(metadata))
        assert result.success is True, result.message
        assert image_edits.insert.call_count == 1
        assert image_edits.commit.call_count == 1

        updates = {
            c.kwargs["language"]: c.kwargs["body"]
            for c in image_edits.listings.return_value.update.call_args_list
        }
        # Provided fields override; missing ones keep Play's current value.
        assert updates["en-US"] == {
            "title": "My App",
            "shortDescription": "Short and sweet",
            "fullDescription": "Old full",
        }
        assert updates["de-DE"]["fullDescription"] == "Lange Beschreibung"
        assert updates["de-DE"]["title"] == "Old title"

        uploads = [
            c.kwargs["media_body"]._filename
            for c in image_edits.images.return_value.upload.call_args_list
            if c.kwargs["imageType"] == "phoneScreenshots"
        ]
        assert [u.rsplit("/", 1)[-1] for u in uploads] == ["1.png", "2.png", "10.png"]
        en = next(lang for lang in result.languages if lang.language == "en-US")
        assert en.deleted_images == {"icon": 2, "phoneScreenshots": 2}

    def test_language_filter(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        result = client.sync_listing_from_directory(
            PKG, str(metadata), languages=["de-DE"], replace_images=False
        )
        assert result.success is True
        assert [lang.language for lang in result.languages] == ["de-DE"]
        image_edits.images.return_value.upload.assert_not_called()

    def test_unknown_language_filter(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        result = client.sync_listing_from_directory(PKG, str(metadata), languages=["fr-FR"])
        assert result.success is False
        assert "fr-FR" in result.message
        image_edits.insert.assert_not_called()

    def test_validation_problems_block_sync(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        (metadata / "en-US" / "title.txt").write_text("T" * 31, encoding="utf-8")
        result = client.sync_listing_from_directory(PKG, str(metadata))
        assert result.success is False
        assert any("en-US" in p and "30" in p for p in result.problems)
        image_edits.insert.assert_not_called()

    def test_listing_read_error_does_not_blank_fields(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        image_edits.listings.return_value.get.return_value.execute.side_effect = _http_error(
            403, "Forbidden"
        )
        result = client.sync_listing_from_directory(PKG, str(metadata))
        assert result.success is False
        image_edits.listings.return_value.update.assert_not_called()
        image_edits.commit.assert_not_called()

    def test_new_language_404_starts_empty(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        image_edits.listings.return_value.get.return_value.execute.side_effect = _http_error(
            404, "Not found"
        )
        result = client.sync_listing_from_directory(PKG, str(metadata), languages=["en-US"])
        assert result.success is True, result.message
        body = image_edits.listings.return_value.update.call_args.kwargs["body"]
        assert body["title"] == "My App"
        assert body["fullDescription"] == ""

    def test_empty_file_is_skipped_with_warning(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        (metadata / "en-US" / "title.txt").write_text("  \n", encoding="utf-8")
        result = client.sync_listing_from_directory(PKG, str(metadata), languages=["en-US"])
        assert result.success is True, result.message
        assert any("title.txt is empty" in w for w in result.warnings)
        body = image_edits.listings.return_value.update.call_args.kwargs["body"]
        assert body["title"] == "Old title"

    def test_non_utf8_file_is_a_problem(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        (metadata / "de-DE" / "full_description.txt").write_bytes("Größe".encode("utf-16"))
        result = client.sync_listing_from_directory(PKG, str(metadata))
        assert result.success is False
        assert any("de-DE/full_description.txt is not UTF-8" in p for p in result.problems)
        image_edits.insert.assert_not_called()

    def test_missing_directory(self, client: PlayStoreClient, tmp_path: Any) -> None:
        result = client.sync_listing_from_directory(PKG, str(tmp_path / "nope"))
        assert result.success is False
        assert result.error == "NotADirectory"

    def test_empty_directory(self, client: PlayStoreClient, tmp_path: Any) -> None:
        (tmp_path / "en-US").mkdir()
        result = client.sync_listing_from_directory(PKG, str(tmp_path))
        assert result.success is False
        assert result.error == "NothingToSync"

    def test_api_failure_discards_edit(
        self, client: PlayStoreClient, image_edits: MagicMock, metadata: Any
    ) -> None:
        image_edits.listings.return_value.update.return_value.execute.side_effect = _http_error(
            400, "Bad listing"
        )
        result = client.sync_listing_from_directory(PKG, str(metadata))
        assert result.success is False
        assert "no changes were committed" in result.message
        image_edits.commit.assert_not_called()
        image_edits.delete.assert_called()
        assert result.languages  # plan is still reported


# =========================================================================
# server tools
# =========================================================================


class TestWorkflowTools:
    @pytest.fixture(autouse=True)
    def _route(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        mock = MagicMock(spec=PlayStoreClient)
        monkeypatch.setattr(server, "get_http_headers", dict)
        monkeypatch.setitem(server._shared_state, "client", mock)
        monkeypatch.setattr(server, "READ_ONLY", False)
        self.mock = mock
        return mock

    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("create_one_time_product_with_price", (PKG, "x", "t", "d", "1")),
            ("batch_create_one_time_products_with_price", (PKG, [])),
            ("create_subscription_with_price", (PKG, "x", "t", "1")),
            ("replace_listing_images", (PKG, "en-US", "icon", ["/a.png"])),
            ("sync_listing_from_directory", (PKG, "/meta")),
            ("create_free_trial_offer", (PKG, "premium", "monthly")),
        ],
    )
    def test_writes_blocked_in_read_only(
        self, monkeypatch: pytest.MonkeyPatch, tool: str, args: tuple[Any, ...]
    ) -> None:
        monkeypatch.setattr(server, "READ_ONLY", True)
        result = getattr(server, tool)(*args)
        assert "read-only" in result["error"]
        assert not self.mock.method_calls

    def test_dry_run_allowed_in_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "READ_ONLY", True)
        self.mock.sync_listing_from_directory.return_value = ListingSyncResult(
            success=True, message="ok", package_name=PKG, directory="/meta", dry_run=True
        )
        result = server.sync_listing_from_directory(PKG, "/meta", dry_run=True)
        assert result["success"] is True

    def test_convert_allowed_in_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "READ_ONLY", True)
        self.mock.convert_region_prices.return_value = RegionalPriceConversion(
            package_name=PKG, base_price="1 USD", regions_version="2025/03"
        )
        assert server.convert_region_prices(PKG, "1")["regions_version"] == "2025/03"

    @pytest.mark.asyncio
    async def test_price_accepts_json_number(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from fastmcp import Client

        # The in-memory client runs the lifespan, which resets the shared client.
        monkeypatch.setattr(server, "get_client_from_context", lambda: self.mock)

        self.mock.convert_region_prices.return_value = RegionalPriceConversion(
            package_name=PKG, base_price="0.99 USD", regions_version="2025/03"
        )
        async with Client(server.mcp) as c:
            result = await c.call_tool(
                "convert_region_prices", {"package_name": PKG, "price": 0.99}
            )
        assert not result.is_error
        assert self.mock.convert_region_prices.call_args.args[1] == 0.99

    def test_deploy_rejects_conflicting_release_status(self, tmp_path: Any) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        result = server.deploy_app(
            PKG, "production", str(aab), rollout_percentage=5.0, release_status="completed"
        )
        assert "100% of users" in result["error"]
        self.mock.deploy_app.assert_not_called()

    def test_deploy_rejects_bad_release_status(self, tmp_path: Any) -> None:
        aab = tmp_path / "app.aab"
        aab.write_bytes(b"aab")
        result = server.deploy_app(PKG, "internal", str(aab), release_status="live")
        assert "release_status" in result["error"]
        self.mock.deploy_app.assert_not_called()

    def test_create_subscription_tool_forwards(self) -> None:
        self.mock.create_subscription_with_price.return_value = ProductSetupResult(
            success=True,
            message="ok",
            package_name=PKG,
            product_id="premium",
            product_kind="subscription",
        )
        result = server.create_subscription_with_price(PKG, "premium", "Premium", "4.99")
        assert result["success"] is True
        kwargs = self.mock.create_subscription_with_price.call_args.kwargs
        assert kwargs["billing_period"] == "P1M"
        assert kwargs["base_plan_id"] == "monthly"
        assert kwargs["free_trial"] is None

    def test_create_free_trial_offer_tool_forwards(self) -> None:
        from play_store_mcp.models import OfferSetupResult

        self.mock.create_free_trial_offer.return_value = OfferSetupResult(
            success=True,
            message="ok",
            package_name=PKG,
            product_id="premium",
            base_plan_id="monthly",
            offer_id="free-trial",
        )
        result = server.create_free_trial_offer(PKG, "premium", "monthly", trial_duration="P1M")
        assert result["success"] is True
        assert self.mock.create_free_trial_offer.call_args.kwargs["trial_duration"] == "P1M"
