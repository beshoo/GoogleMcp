"""Tests for fixes found by testing against a live Play Console account:

- reads share one cached edit (Play's "Daily edit creation quota exceeded")
- the retired legacy in-app products API gets an actionable error
- PLAY_STORE_MCP_ALLOWED_PACKAGES limits the server to specific apps
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import Client, FastMCP
from googleapiclient.errors import HttpError

from play_store_mcp import client as client_module
from play_store_mcp import server
from play_store_mcp.client import PlayStoreClient, PlayStoreClientError
from play_store_mcp.package_guard import (
    ALLOWED_ENV,
    PackageAllowlistMiddleware,
    allowed_packages,
    check_call,
)

PKG = "flona.app"


def _http_error(status: int, message: str) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    return HttpError(resp=resp, content=message.encode())


@pytest.fixture
def edits(_mock_service: MagicMock) -> MagicMock:
    edits = _mock_service.edits.return_value
    edits.insert.return_value.execute.side_effect = [{"id": f"edit-{i}"} for i in range(1, 10)]
    edits.get.return_value.execute.return_value = {
        "id": "edit-1",
        "expiryTimeSeconds": str(int(time.time()) + 3600),
    }
    edits.listings.return_value.list.return_value.execute.return_value = {"listings": []}
    edits.images.return_value.list.return_value.execute.return_value = {"images": []}
    edits.tracks.return_value.list.return_value.execute.return_value = {"tracks": []}
    edits.commit.return_value.execute.return_value = {}
    return edits


# =========================================================================
# shared read edit
# =========================================================================


class TestSharedReadEdit:
    def test_reads_reuse_one_edit(self, client: PlayStoreClient, edits: MagicMock) -> None:
        client.get_releases(PKG)
        client.list_all_listings(PKG)
        for image_type in ("icon", "featureGraphic", "phoneScreenshots"):
            client.list_images(PKG, "en-US", image_type)

        assert edits.insert.call_count == 1
        assert edits.delete.call_count == 0
        used = {c.kwargs["editId"] for c in edits.images.return_value.list.call_args_list}
        assert used == {"edit-1"}
        # Liveness is checked before each reuse (a read; not quota-limited).
        assert edits.get.call_count == 4

    def test_dead_edit_is_replaced(self, client: PlayStoreClient, edits: MagicMock) -> None:
        client.get_releases(PKG)
        edits.get.return_value.execute.side_effect = _http_error(400, "This Edit has been deleted.")
        client.get_releases(PKG)
        assert edits.insert.call_count == 2
        assert edits.tracks.return_value.list.call_args.kwargs["editId"] == "edit-2"

    def test_edit_near_expiry_is_replaced(self, client: PlayStoreClient, edits: MagicMock) -> None:
        client.get_releases(PKG)
        edits.get.return_value.execute.return_value = {
            "expiryTimeSeconds": str(int(time.time()) + 10)
        }
        client.get_releases(PKG)
        assert edits.insert.call_count == 2

    def test_ttl_expiry_replaces_and_tidies_old_edit(
        self, client: PlayStoreClient, edits: MagicMock
    ) -> None:
        client.get_releases(PKG)
        real = time.monotonic
        with patch.object(
            client_module.time,
            "monotonic",
            lambda: real() + client_module._READ_EDIT_TTL_SECONDS + 1,
        ):
            client.get_releases(PKG)
        assert edits.insert.call_count == 2
        edits.delete.assert_called_once_with(packageName=PKG, editId="edit-1")

    def test_commit_drops_cached_edit(self, client: PlayStoreClient, edits: MagicMock) -> None:
        client.get_releases(PKG)
        client._commit_edit(PKG, "write-edit")
        client.get_releases(PKG)
        assert edits.insert.call_count == 2

    def test_cache_is_per_package(self, client: PlayStoreClient, edits: MagicMock) -> None:
        client.get_releases(PKG)
        client.get_releases("com.other.app")
        assert edits.insert.call_count == 2

    def test_read_error_keeps_working_edit(self, client: PlayStoreClient, edits: MagicMock) -> None:
        edits.tracks.return_value.list.return_value.execute.side_effect = [
            _http_error(500, "backend"),
            {"tracks": []},
        ]
        with pytest.raises(PlayStoreClientError):
            client.get_releases(PKG)
        client.get_releases(PKG)
        assert edits.insert.call_count == 1


# =========================================================================
# legacy in-app products API
# =========================================================================


class TestLegacyInAppProductsHint:
    MIGRATE = "Please migrate to the new publishing API."

    def test_list_error_points_to_new_tools(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        _mock_service.inappproducts.return_value.list.return_value.execute.side_effect = (
            _http_error(403, self.MIGRATE)
        )
        with pytest.raises(PlayStoreClientError, match="list_one_time_products"):
            client.list_in_app_products(PKG)

    def test_delete_result_points_to_new_tools(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        _mock_service.inappproducts.return_value.delete.return_value.execute.side_effect = (
            _http_error(403, self.MIGRATE)
        )
        result = client.delete_in_app_product(PKG, "coins")
        assert result.success is False
        assert "create_one_time_product_with_price" in result.message

    def test_other_errors_unchanged(
        self, client: PlayStoreClient, _mock_service: MagicMock
    ) -> None:
        _mock_service.inappproducts.return_value.list.return_value.execute.side_effect = (
            _http_error(403, "Forbidden")
        )
        with pytest.raises(PlayStoreClientError) as exc:
            client.list_in_app_products(PKG)
        assert "one_time" not in str(exc.value)

    async def test_legacy_tools_are_marked_deprecated(self) -> None:
        tools = {t.name: t for t in await server.mcp.list_tools()}
        for name in ("list_in_app_products", "create_in_app_product", "delete_in_app_product"):
            assert "DEPRECATED" in (tools[name].description or "")


# =========================================================================
# package allowlist
# =========================================================================


class TestAllowedPackages:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", None),
            ("   ", None),
            ("flona.app", frozenset({"flona.app"})),
            ("flona.app, com.b.c  com.d.e", frozenset({"flona.app", "com.b.c", "com.d.e"})),
        ],
    )
    def test_parsing(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: frozenset[str] | None
    ) -> None:
        monkeypatch.setenv(ALLOWED_ENV, raw)
        assert allowed_packages() == expected

    def test_check_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLOWED_ENV, "flona.app")
        assert check_call("get_releases", {"package_name": "flona.app"}) is None
        assert "not allowed" in (check_call("get_releases", {"package_name": "com.x"}) or "")
        assert check_call("validate_track", {"track": "beta"}) is None
        assert check_call("list_users", {"developer_id": "1"}) is None
        assert "every app" in (check_call("create_user", {"developer_id": "1"}) or "")

    def test_no_allowlist_allows_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ALLOWED_ENV, raising=False)
        assert check_call("create_user", {}) is None
        assert check_call("get_releases", {"package_name": "anything"}) is None


class TestAllowlistMiddleware:
    @pytest.fixture
    def probe(self) -> tuple[FastMCP, list[str]]:
        calls: list[str] = []
        probe = FastMCP("probe")
        probe.add_middleware(PackageAllowlistMiddleware())

        @probe.tool
        def get_releases(package_name: str) -> dict[str, Any]:
            calls.append(package_name)
            return {"ok": True}

        return probe, calls

    async def test_blocks_other_packages(
        self, probe: tuple[FastMCP, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ALLOWED_ENV, "flona.app")
        app, calls = probe
        async with Client(app) as c:
            ok = await c.call_tool("get_releases", {"package_name": "flona.app"})
            blocked = await c.call_tool(
                "get_releases", {"package_name": "com.hala.instgram"}, raise_on_error=False
            )
        assert not ok.is_error
        assert blocked.is_error
        assert "not allowed" in blocked.content[0].text
        assert calls == ["flona.app"]

    async def test_blocks_inner_code_mode_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLOWED_ENV, "flona.app")
        monkeypatch.setenv("CODE_MODE", "1")
        calls: list[str] = []
        app = FastMCP("probe", transforms=server._build_transforms())
        app.add_middleware(PackageAllowlistMiddleware())

        @app.tool
        def get_releases(package_name: str) -> dict[str, Any]:
            calls.append(package_name)
            return {"ok": True}

        async with Client(app) as c:
            await c.call_tool(
                "execute",
                {"code": "return await call_tool('get_releases', {'package_name': 'com.other'})"},
                raise_on_error=False,
            )
        assert calls == []

    async def test_real_server_registers_middleware(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ALLOWED_ENV, "flona.app")
        monkeypatch.setattr(server, "get_client_from_context", MagicMock())
        async with Client(server.mcp) as c:
            result = await c.call_tool(
                "get_releases", {"package_name": "com.hala.instgram"}, raise_on_error=False
            )
        assert result.is_error
        assert "flona.app" in result.content[0].text
        server.get_client_from_context.assert_not_called()  # type: ignore[attr-defined]


# =========================================================================
# update_app_details
# =========================================================================


class TestUpdateAppDetails:
    @pytest.fixture
    def details(self, edits: MagicMock) -> MagicMock:
        details = edits.details.return_value
        details.patch.return_value.execute.return_value = {
            "contactEmail": "hello@flona.app",
            "contactWebsite": "https://flona.app",
            "defaultLanguage": "en-US",
        }
        return details

    def test_fix_email(self, client: PlayStoreClient, edits: MagicMock, details: MagicMock) -> None:
        result = client.update_app_details(PKG, contact_email=" hello@flona.app ")
        assert result.success is True, result.message
        assert result.updated_fields == ["contact_email"]
        assert result.contact_email == "hello@flona.app"
        assert result.sent_for_review is True
        assert details.patch.call_args.kwargs["body"] == {"contactEmail": "hello@flona.app"}
        assert edits.commit.call_count == 1

    @pytest.mark.parametrize(
        ("kwargs", "fragment"),
        [
            ({}, "Nothing to update"),
            ({"contact_email": "hello@flona"}, "not a valid email"),
            ({"contact_website": "flona.app"}, "http"),
            ({"default_language": "English"}, "BCP-47"),
        ],
    )
    def test_validation(
        self, client: PlayStoreClient, edits: MagicMock, kwargs: dict[str, Any], fragment: str
    ) -> None:
        result = client.update_app_details(PKG, **kwargs)
        assert result.success is False
        assert fragment in result.message
        edits.insert.assert_not_called()

    def test_clear_phone_and_website(self, client: PlayStoreClient, details: MagicMock) -> None:
        client.update_app_details(PKG, contact_phone="", contact_website="")
        assert details.patch.call_args.kwargs["body"] == {"contactWebsite": "", "contactPhone": ""}

    def test_api_failure_discards_edit(
        self, client: PlayStoreClient, edits: MagicMock, details: MagicMock
    ) -> None:
        details.patch.return_value.execute.side_effect = _http_error(400, "Invalid email")
        result = client.update_app_details(PKG, contact_email="a@b.co")
        assert result.success is False
        edits.commit.assert_not_called()
        edits.delete.assert_called()

    def test_tool_blocked_in_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "READ_ONLY", True)
        result = server.update_app_details(PKG, contact_email="hello@flona.app")
        assert "read-only" in result["error"]
