"""Tests for multi-track deploys (upload once, one commit) and custom track names."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

if TYPE_CHECKING:
    from play_store_mcp.client import PlayStoreClient

PKG = "flona.app"


def _http_error(status: int, message: str) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = message
    return HttpError(resp=resp, content=message.encode())


@pytest.fixture
def edits(_mock_service: MagicMock) -> MagicMock:
    edits = _mock_service.edits.return_value
    edits.insert.return_value.execute.return_value = {"id": "edit-1"}
    edits.bundles.return_value.upload.return_value.execute.return_value = {"versionCode": 42}
    edits.tracks.return_value.update.return_value.execute.return_value = {}
    edits.commit.return_value.execute.return_value = {}
    edits.tracks.return_value.list.return_value.execute.return_value = {
        "tracks": [{"track": t} for t in ["production", "beta", "alpha", "internal", "P1"]]
    }
    return edits


@pytest.fixture
def aab(tmp_path: Any) -> str:
    path = tmp_path / "app.aab"
    path.write_bytes(b"aab")
    return str(path)


class TestBatchDeploy:
    def test_uploads_once_and_commits_once(
        self, client: PlayStoreClient, edits: MagicMock, aab: str
    ) -> None:
        result = client.batch_deploy(
            PKG,
            aab,
            ["internal", "P1", "internal"],
            release_notes={"en-US": "Fixes", "ar": "إصلاحات"},
            rollout_percentages={"P1": 20.0},
        )
        assert result.success is True, result.message
        assert edits.bundles.return_value.upload.call_count == 1
        assert edits.insert.call_count == 1
        assert edits.commit.call_count == 1
        assert [r.track for r in result.results] == ["internal", "P1"]
        assert {r.version_code for r in result.results} == {42}
        updates = {
            c.kwargs["track"]: c.kwargs["body"]["releases"][0]
            for c in edits.tracks.return_value.update.call_args_list
        }
        assert updates["internal"]["status"] == "completed"
        assert updates["P1"]["status"] == "inProgress"
        assert updates["P1"]["userFraction"] == pytest.approx(0.2)
        assert len(updates["P1"]["releaseNotes"]) == 2
        assert "once" in result.message
        assert "20%" in result.results[1].message

    def test_draft_app_fallback_applies_to_all_tracks(
        self, client: PlayStoreClient, edits: MagicMock, aab: str
    ) -> None:
        edits.commit.return_value.execute.side_effect = [
            _http_error(400, "Only releases with status draft may be created on draft app."),
            {},
        ]
        result = client.batch_deploy(PKG, aab, ["internal", "P1"])
        assert result.success is True
        assert "draft" in result.message
        last_two = edits.tracks.return_value.update.call_args_list[-2:]
        assert {c.kwargs["body"]["releases"][0]["status"] for c in last_two} == {"draft"}

    def test_upload_failure_releases_nothing(
        self, client: PlayStoreClient, edits: MagicMock, aab: str
    ) -> None:
        edits.bundles.return_value.upload.return_value.execute.side_effect = _http_error(
            400, "Version code 42 has already been used."
        )
        result = client.batch_deploy(PKG, aab, ["internal", "P1"])
        assert result.success is False
        assert result.failed_count == 2
        assert "already been used" in result.message
        edits.tracks.return_value.update.assert_not_called()
        edits.commit.assert_not_called()

    def test_rollout_for_unknown_track_rejected(
        self, client: PlayStoreClient, edits: MagicMock, aab: str
    ) -> None:
        result = client.batch_deploy(PKG, aab, ["internal"], rollout_percentages={"beta": 10.0})
        assert result.success is False
        assert "beta" in result.message
        edits.insert.assert_not_called()

    def test_missing_file(self, client: PlayStoreClient, edits: MagicMock) -> None:
        result = client.batch_deploy(PKG, "/nope/app.aab", ["internal"])
        assert result.success is False
        assert "File not found" in result.message
        edits.insert.assert_not_called()


class TestValidateTrack:
    @pytest.mark.parametrize("track", ["internal", "production", "P1", "wear:production"])
    def test_format_only_accepts_any_name(self, client: PlayStoreClient, track: str) -> None:
        assert client.validate_track(track) == []

    def test_custom_track_exists(self, client: PlayStoreClient, edits: MagicMock) -> None:
        assert client.validate_track("P1", PKG) == []
        edits.tracks.return_value.list.assert_called_once()

    @pytest.mark.usefixtures("edits")
    def test_unknown_track_lists_existing(self, client: PlayStoreClient) -> None:
        errors = client.validate_track("staging", PKG)
        assert len(errors) == 1
        assert "does not exist in flona.app" in errors[0].message
        assert "P1" in errors[0].message

    @pytest.mark.usefixtures("edits")
    def test_case_mismatch_suggests(self, client: PlayStoreClient) -> None:
        errors = client.validate_track("p1", PKG)
        assert "Did you mean 'P1'?" in errors[0].message

    def test_too_long(self, client: PlayStoreClient) -> None:
        errors = client.validate_track("x" * 101)
        assert "100 characters" in errors[0].message

    @pytest.mark.usefixtures("edits")
    def test_list_track_names(self, client: PlayStoreClient) -> None:
        assert client.list_track_names(PKG) == ["production", "beta", "alpha", "internal", "P1"]
