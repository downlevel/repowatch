"""Minimal GitHub REST client: auth, pagination, retries and rate-limit handling."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)

STAR_ACCEPT = "application/vnd.github.star+json"


class GitHubError(Exception):
    def __init__(self, status: int, url: str, message: str):
        super().__init__(f"{status} {url}: {message}")
        self.status = status


class GitHub:
    def __init__(self, token: str, api_url: str = "https://api.github.com", timeout: float = 30):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "repowatch",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=api_url, headers=headers, timeout=timeout)
        self.requests = 0

    def close(self) -> None:
        self._client.close()

    def _request(self, url: str, params: dict | None = None, accept: str | None = None) -> httpx.Response:
        headers = {"Accept": accept} if accept else None
        for attempt in range(4):
            try:
                resp = self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as exc:
                if attempt == 3:
                    raise
                log.warning("transport error on %s (%s), retrying", url, exc)
                time.sleep(2**attempt)
                continue
            self.requests += 1

            remaining = resp.headers.get("x-ratelimit-remaining")
            if resp.status_code in (403, 429) and remaining == "0":
                wait = int(resp.headers.get("x-ratelimit-reset", "0")) - int(time.time()) + 1
                if 0 < wait <= 900:
                    log.warning("rate limit hit, sleeping %ss", wait)
                    time.sleep(wait)
                    continue
                raise GitHubError(resp.status_code, url, "rate limit exceeded")
            if resp.status_code in (429,) or resp.status_code >= 500:
                if attempt < 3:
                    time.sleep(int(resp.headers.get("retry-after", 2**attempt)))
                    continue
            if resp.status_code >= 400:
                try:
                    message = resp.json().get("message", resp.text)
                except ValueError:
                    message = resp.text
                raise GitHubError(resp.status_code, url, message)
            return resp
        raise GitHubError(resp.status_code, url, "giving up after retries")

    def get(self, url: str, params: dict | None = None, accept: str | None = None) -> Any:
        return self._request(url, params, accept).json()

    def paginate(
        self, url: str, params: dict | None = None, accept: str | None = None, item_key: str | None = None
    ) -> Iterator[Any]:
        """Yield items across pages following the Link header.

        `item_key` handles endpoints that wrap the list in an object (e.g. workflow_runs).
        """
        params = {"per_page": 100, **(params or {})}
        next_url: str | None = url
        while next_url:
            resp = self._request(next_url, params, accept)
            data = resp.json()
            yield from (data[item_key] if item_key else data)
            next_url = resp.links.get("next", {}).get("url")
            params = None  # the next link already carries the query string
