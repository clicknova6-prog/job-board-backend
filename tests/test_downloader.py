"""Unit tests for secure provider feed download and ZIP extraction."""

from __future__ import annotations

import ipaddress
import socket
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Self
from unittest.mock import Mock, call, sentinel
from urllib.error import URLError

import pytest

from app.imports import downloader
from app.imports.exceptions import InvalidFeedArchiveError, TransientImportError


class _FakeHTTPResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.headers = headers or {}
        self._stream = BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self._stream.close()


def _provider(
    *,
    retry_max_attempts: object = 3,
    config: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        feed_url="https://feeds.example/jobs.zip",
        timeout_seconds=5,
        retry_max_attempts=retry_max_attempts,
        config=config or {},
    )


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, contents in files.items():
            archive.writestr(filename, contents)
    return output.getvalue()


def _mock_http_response(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> Mock:
    opener = Mock()
    opener.open.return_value = _FakeHTTPResponse(payload, headers=headers)
    monkeypatch.setattr(downloader, "build_opener", Mock(return_value=opener))
    return opener


def _use_temporary_directory(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
) -> None:
    directory.mkdir()
    monkeypatch.setattr(
        downloader.tempfile,
        "mkdtemp",
        Mock(return_value=str(directory)),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/feed.zip",
        "https://subdomain.localhost/feed.zip",
        "http://127.0.0.1/feed.zip",
        "http://10.20.30.40/feed.zip",
        "http://172.16.0.1/feed.zip",
        "http://172.31.255.254/feed.zip",
        "http://192.168.1.10/feed.zip",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/feed.zip",
        "ftp://203.0.113.1/feed.zip",
        "file:///etc/passwd",
    ],
)
def test_non_public_or_non_http_urls_are_rejected_before_dns_or_connection(
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    getaddrinfo = Mock(side_effect=AssertionError("unexpected DNS lookup"))
    create_connection = Mock(side_effect=AssertionError("unexpected connection"))
    monkeypatch.setattr(downloader.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(downloader.socket, "create_connection", create_connection)

    with pytest.raises(ValueError):
        downloader._resolve_public_http_url(url)
    with pytest.raises(ValueError):
        downloader._validate_public_http_url(url)

    getaddrinfo.assert_not_called()
    create_connection.assert_not_called()


def test_hostname_resolving_to_private_address_is_rejected_without_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    getaddrinfo = Mock(
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", 443),
            )
        ]
    )
    create_connection = Mock(side_effect=AssertionError("unexpected connection"))
    monkeypatch.setattr(downloader.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(downloader.socket, "create_connection", create_connection)

    with pytest.raises(ValueError, match="non-public address"):
        downloader._resolve_public_http_url("https://feeds.example/jobs.zip")

    getaddrinfo.assert_called_once()
    create_connection.assert_not_called()


def test_hostname_with_any_non_public_dns_answer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        downloader.socket,
        "getaddrinfo",
        Mock(
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.4", 443)),
            ]
        ),
    )

    with pytest.raises(ValueError, match="non-public address"):
        downloader._resolve_public_http_url("https://feeds.example/jobs.zip")


def test_hostname_resolving_to_public_ip_is_validated_and_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    getaddrinfo = Mock(
        return_value=[
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 8443),
            )
        ]
    )
    monkeypatch.setattr(downloader.socket, "getaddrinfo", getaddrinfo)

    target = downloader._resolve_public_http_url(
        "https://Feeds.Example.:8443/jobs.zip"
    )

    assert target.hostname == "feeds.example"
    assert target.port == 8443
    assert target.address == ipaddress.ip_address("93.184.216.34")
    downloader._validate_public_http_url("https://Feeds.Example.:8443/jobs.zip")
    assert getaddrinfo.call_count == 2


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("https://user:secret@feeds.example/feed.zip", "user credentials"),
        ("https://feeds.example:invalid/feed.zip", "invalid port"),
    ],
)
def test_url_credentials_and_invalid_ports_are_rejected(
    url: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        downloader._resolve_public_http_url(url)


def test_dns_resolution_failure_is_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_error = OSError("temporary resolver failure")
    monkeypatch.setattr(
        downloader.socket,
        "getaddrinfo",
        Mock(side_effect=dns_error),
    )

    with pytest.raises(TransientImportError, match="Could not resolve") as exc_info:
        downloader._resolve_public_http_url("https://feeds.example/feed.zip")

    assert exc_info.value.__cause__ is dns_error


def test_connection_mixin_substitutes_pinned_address_and_restores_connector() -> None:
    original_connector = Mock(return_value=sentinel.socket)

    class _ConnectionBase:
        def __init__(self) -> None:
            self._create_connection = original_connector

        def connect(self) -> None:
            self.socket = self._create_connection(
                ("feeds.example", 443),
                timeout=7,
                source_address=("0.0.0.0", 0),
            )

    class _PinnedConnection(
        downloader._ResolvedAddressConnectionMixin,
        _ConnectionBase,
    ):
        pass

    connection = _PinnedConnection(resolved_address="93.184.216.34")

    connection.connect()

    assert connection.socket is sentinel.socket
    original_connector.assert_called_once_with(
        ("93.184.216.34", 443),
        7,
        ("0.0.0.0", 0),
    )
    assert connection._create_connection is original_connector


def test_redirect_destination_is_validated_and_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = downloader._ResolvedTarget(
        url="https://cdn.example/feed.zip",
        hostname="cdn.example",
        port=443,
        address=ipaddress.ip_address("93.184.216.34"),
    )
    resolve = Mock(return_value=target)
    monkeypatch.setattr(downloader, "_resolve_public_http_url", resolve)

    redirected = downloader._ValidatedRedirectHandler().redirect_request(
        downloader.Request("https://feeds.example/feed.zip"),
        sentinel.response,
        302,
        "Found",
        {},
        target.url,
    )

    assert redirected is not None
    assert redirected.full_url == target.url
    assert getattr(redirected, downloader._RESOLVED_TARGET_ATTRIBUTE) is target
    resolve.assert_called_once_with(target.url)


def test_redirect_to_private_address_is_rejected_before_request_creation() -> None:
    handler = downloader._ValidatedRedirectHandler()

    with pytest.raises(ValueError, match="non-public address"):
        handler.redirect_request(
            downloader.Request("https://feeds.example/feed.zip"),
            sentinel.response,
            302,
            "Found",
            {},
            "http://169.254.169.254/latest/meta-data/",
        )


def test_http_and_https_handlers_use_the_pinned_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http_target = downloader._ResolvedTarget(
        "http://feeds.example/feed.zip",
        "feeds.example",
        80,
        ipaddress.ip_address("93.184.216.34"),
    )
    https_target = downloader._ResolvedTarget(
        "https://feeds.example/feed.zip",
        "feeds.example",
        443,
        ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"),
    )
    http_connection = Mock(return_value=sentinel.http_connection)
    https_connection = Mock(return_value=sentinel.https_connection)
    monkeypatch.setattr(downloader, "_ResolvedHTTPConnection", http_connection)
    monkeypatch.setattr(downloader, "_ResolvedHTTPSConnection", https_connection)

    http_request = downloader.Request(http_target.url)
    https_request = downloader.Request(https_target.url)
    setattr(http_request, downloader._RESOLVED_TARGET_ATTRIBUTE, http_target)
    setattr(https_request, downloader._RESOLVED_TARGET_ATTRIBUTE, https_target)

    http_handler = downloader._ResolvedHTTPHandler()
    https_handler = downloader._ResolvedHTTPSHandler()

    def use_http_factory(factory: object, request: object) -> object:
        assert request is http_request
        return factory("ignored", timeout=12)  # type: ignore[operator]

    def use_https_factory(
        factory: object,
        request: object,
        **kwargs: object,
    ) -> object:
        assert request is https_request
        assert kwargs == {
            "context": https_handler._context,
            "check_hostname": https_handler._check_hostname,
        }
        return factory("ignored", timeout=13)  # type: ignore[operator]

    monkeypatch.setattr(http_handler, "do_open", use_http_factory)
    monkeypatch.setattr(https_handler, "do_open", use_https_factory)

    assert http_handler.http_open(http_request) is sentinel.http_connection
    assert https_handler.https_open(https_request) is sentinel.https_connection
    http_connection.assert_called_once_with(
        "feeds.example",
        port=80,
        timeout=12,
        resolved_address="93.184.216.34",
    )
    https_connection.assert_called_once_with(
        "feeds.example",
        port=443,
        timeout=13,
        resolved_address="2606:2800:220:1:248:1893:25c8:1946",
    )


def test_request_without_a_pinned_target_is_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = downloader.Request("https://feeds.example/feed.zip")
    target = downloader._ResolvedTarget(
        request.full_url,
        "feeds.example",
        443,
        ipaddress.ip_address("93.184.216.34"),
    )
    resolve = Mock(return_value=target)
    monkeypatch.setattr(downloader, "_resolve_public_http_url", resolve)

    assert downloader._target_for_request(request) is target
    resolve.assert_called_once_with(request.full_url)


def test_transient_download_retries_with_exponential_capped_backoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temporary_directory = tmp_path / "successful-retry"
    _use_temporary_directory(monkeypatch, temporary_directory)
    service = downloader.DownloadService()
    download_zip = Mock(
        side_effect=[
            TransientImportError("failure 1"),
            TransientImportError("failure 2"),
            TransientImportError("failure 3"),
            None,
        ]
    )
    extract_xml = Mock()
    sleep = Mock()
    monkeypatch.setattr(service, "_download_zip", download_zip)
    monkeypatch.setattr(service, "_extract_xml", extract_xml)
    monkeypatch.setattr(downloader.time, "sleep", sleep)

    downloaded = service.download(
        _provider(
            retry_max_attempts=4,
            config={
                "download_retry_backoff_seconds": 1,
                "download_retry_backoff_max_seconds": 3,
            },
        )
    )

    assert download_zip.call_count == 4
    assert sleep.call_args_list == [call(1.0), call(2.0), call(3.0)]
    extract_xml.assert_called_once()
    downloaded.cleanup()


def test_retries_exhausted_reraises_and_never_extracts_or_touches_live_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    temporary_directory = tmp_path / "failed-retry"
    _use_temporary_directory(monkeypatch, temporary_directory)
    live_data = tmp_path / "live-feed.xml"
    live_data.write_bytes(b"existing live catalogue")
    service = downloader.DownloadService()
    download_error = TransientImportError("network unavailable")
    download_zip = Mock(side_effect=download_error)
    extract_xml = Mock()
    sleep = Mock()
    monkeypatch.setattr(service, "_download_zip", download_zip)
    monkeypatch.setattr(service, "_extract_xml", extract_xml)
    monkeypatch.setattr(downloader.time, "sleep", sleep)

    with pytest.raises(TransientImportError) as exc_info:
        service.download(_provider(retry_max_attempts=3))

    assert exc_info.value is download_error
    assert download_zip.call_count == 3
    assert sleep.call_args_list == [call(1.0), call(2.0)]
    extract_xml.assert_not_called()
    assert live_data.read_bytes() == b"existing live catalogue"
    assert not temporary_directory.exists()


def test_provider_without_feed_url_is_rejected() -> None:
    provider = _provider()
    provider.feed_url = None

    with pytest.raises(ValueError, match="has no feed_url"):
        downloader.DownloadService().download(provider)


def test_missing_timeout_uses_default() -> None:
    provider = _provider()
    provider.timeout_seconds = None

    assert downloader.DownloadService._timeout_seconds(provider) == 60.0


@pytest.mark.parametrize(
    ("retry_max_attempts", "expected_exception"),
    [
        (None, TypeError),
        ("3", TypeError),
        (0, ValueError),
        (-1, ValueError),
    ],
)
def test_invalid_retry_max_attempts_fails_before_creating_temporary_directory(
    monkeypatch: pytest.MonkeyPatch,
    retry_max_attempts: object,
    expected_exception: type[Exception],
) -> None:
    mkdtemp = Mock(side_effect=AssertionError("temporary directory created"))
    monkeypatch.setattr(downloader.tempfile, "mkdtemp", mkdtemp)

    with pytest.raises(expected_exception, match="retry_max_attempts"):
        downloader.DownloadService().download(
            _provider(retry_max_attempts=retry_max_attempts)
        )

    mkdtemp.assert_not_called()


@pytest.mark.parametrize(
    ("method_name", "config", "expected_exception"),
    [
        ("_timeout_seconds", {"download_timeout_seconds": "5"}, TypeError),
        ("_timeout_seconds", {"download_timeout_seconds": 0}, ValueError),
        (
            "_configured_non_negative_number",
            {"download_retry_backoff_seconds": "1"},
            TypeError,
        ),
        (
            "_configured_non_negative_number",
            {"download_retry_backoff_seconds": -1},
            ValueError,
        ),
        (
            "_configured_positive_integer",
            {"download_max_bytes": 1.5},
            TypeError,
        ),
        (
            "_configured_positive_integer",
            {"download_max_bytes": 0},
            ValueError,
        ),
    ],
)
def test_invalid_download_configuration_is_rejected(
    method_name: str,
    config: dict[str, object],
    expected_exception: type[Exception],
) -> None:
    provider = _provider(config=config)
    method = getattr(downloader.DownloadService, method_name)

    with pytest.raises(expected_exception):
        if method_name == "_timeout_seconds":
            method(provider)
        elif method_name == "_configured_non_negative_number":
            method(provider, "download_retry_backoff_seconds", 1.0)
        else:
            method(provider, "download_max_bytes", 100)


def test_retry_loop_rejects_zero_attempts_defensively(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="retry loop exited unexpectedly"):
        downloader.DownloadService()._download_with_retries(
            "https://feeds.example/feed.zip",
            tmp_path / "feed.zip",
            timeout_seconds=5,
            max_download_bytes=100,
            max_attempts=0,
            backoff_seconds=1,
            backoff_max_seconds=10,
        )


def test_content_length_over_download_limit_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opener = _mock_http_response(
        monkeypatch,
        b"not read",
        headers={"Content-Length": "101"},
    )
    destination = tmp_path / "feed.zip"

    with pytest.raises(InvalidFeedArchiveError, match="maximum size"):
        downloader.DownloadService._download_zip(
            "https://feeds.example/feed.zip",
            destination,
            timeout_seconds=5,
            max_download_bytes=100,
        )

    opener.open.assert_called_once()
    assert destination.read_bytes() == b""


def test_streamed_download_over_limit_without_content_length_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _mock_http_response(monkeypatch, b"123456", headers={})
    destination = tmp_path / "feed.zip"

    with pytest.raises(InvalidFeedArchiveError, match="maximum size"):
        downloader.DownloadService._download_zip(
            "https://feeds.example/feed.zip",
            destination,
            timeout_seconds=5,
            max_download_bytes=5,
        )

    assert destination.read_bytes() == b""


def test_invalid_content_length_falls_back_to_streamed_counting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _mock_http_response(
        monkeypatch,
        b"small archive",
        headers={"Content-Length": "not-a-number"},
    )
    destination = tmp_path / "feed.zip"

    downloader.DownloadService._download_zip(
        "https://feeds.example/feed.zip",
        destination,
        timeout_seconds=5,
        max_download_bytes=100,
    )

    assert destination.read_bytes() == b"small archive"


@pytest.mark.parametrize(
    "network_error",
    [URLError("connection reset"), TimeoutError("timed out")],
)
def test_network_failures_are_wrapped_as_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    network_error: Exception,
) -> None:
    opener = Mock()
    opener.open.side_effect = network_error
    monkeypatch.setattr(downloader, "build_opener", Mock(return_value=opener))

    with pytest.raises(TransientImportError, match="download failed") as exc_info:
        downloader.DownloadService._download_zip(
            "https://feeds.example/feed.zip",
            tmp_path / "feed.zip",
            timeout_seconds=5,
            max_download_bytes=100,
        )

    assert exc_info.value.__cause__ is network_error


def test_existing_transient_error_is_not_double_wrapped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    download_error = TransientImportError("DNS unavailable")
    opener = Mock()
    opener.open.side_effect = download_error
    monkeypatch.setattr(downloader, "build_opener", Mock(return_value=opener))

    with pytest.raises(TransientImportError) as exc_info:
        downloader.DownloadService._download_zip(
            "https://feeds.example/feed.zip",
            tmp_path / "feed.zip",
            timeout_seconds=5,
            max_download_bytes=100,
        )

    assert exc_info.value is download_error


def test_local_write_failure_is_wrapped_as_transient_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _mock_http_response(monkeypatch, b"archive")
    destination = tmp_path / "missing-directory" / "feed.zip"

    with pytest.raises(TransientImportError, match="download failed") as exc_info:
        downloader.DownloadService._download_zip(
            "https://feeds.example/feed.zip",
            destination,
            timeout_seconds=5,
            max_download_bytes=100,
        )

    assert isinstance(exc_info.value.__cause__, OSError)


def test_zip_declaring_xml_over_extracted_limit_is_rejected(tmp_path: Path) -> None:
    zip_path = tmp_path / "feed.zip"
    zip_path.write_bytes(_zip_bytes({"feed.xml": b"123456"}))

    with pytest.raises(InvalidFeedArchiveError, match="maximum extracted size"):
        downloader.DownloadService._extract_xml(
            zip_path,
            tmp_path / "feed.xml",
            max_extracted_bytes=5,
        )


def test_extracted_stream_over_limit_is_rejected_even_if_size_was_underreported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    member = SimpleNamespace(
        filename="feed.xml",
        file_size=1,
        is_dir=lambda: False,
    )
    archive = Mock()
    archive.__enter__ = Mock(return_value=archive)
    archive.__exit__ = Mock(return_value=None)
    archive.infolist.return_value = [member]
    archive.open.return_value = BytesIO(b"123456")
    monkeypatch.setattr(downloader.zipfile, "ZipFile", Mock(return_value=archive))

    with pytest.raises(InvalidFeedArchiveError, match="maximum extracted size"):
        downloader.DownloadService._extract_xml(
            tmp_path / "feed.zip",
            tmp_path / "feed.xml",
            max_extracted_bytes=5,
        )


def test_valid_small_download_and_extract_succeeds_and_context_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    xml = b"<Jobs><Job /></Jobs>"
    archive = _zip_bytes({"nested/snapshot.XML": xml})
    _mock_http_response(
        monkeypatch,
        archive,
        headers={"Content-Length": str(len(archive))},
    )
    temporary_directory = tmp_path / "successful-download"
    _use_temporary_directory(monkeypatch, temporary_directory)

    with downloader.DownloadService().download(
        _provider(
            config={
                "download_max_bytes": len(archive) + 1,
                "download_max_extracted_bytes": len(xml) + 1,
            }
        )
    ) as downloaded:
        assert downloaded.zip_path.read_bytes() == archive
        assert downloaded.xml_path.read_bytes() == xml
        assert temporary_directory.exists()

    assert not temporary_directory.exists()


def test_corrupt_zip_raises_and_download_failure_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _mock_http_response(monkeypatch, b"not a zip archive")
    temporary_directory = tmp_path / "corrupt-download"
    _use_temporary_directory(monkeypatch, temporary_directory)

    with pytest.raises(InvalidFeedArchiveError, match="not a readable ZIP archive"):
        downloader.DownloadService().download(_provider())

    assert not temporary_directory.exists()


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"README.txt": b"no XML here"}, "does not contain an XML feed"),
        (
            {"first.xml": b"<Jobs />", "second.XML": b"<Jobs />"},
            "more than one XML file",
        ),
    ],
)
def test_zip_must_contain_exactly_one_xml_file(
    tmp_path: Path,
    files: dict[str, bytes],
    message: str,
) -> None:
    zip_path = tmp_path / "feed.zip"
    zip_path.write_bytes(_zip_bytes(files))

    with pytest.raises(InvalidFeedArchiveError, match=message):
        downloader.DownloadService._extract_xml(
            zip_path,
            tmp_path / "feed.xml",
            max_extracted_bytes=100,
        )
