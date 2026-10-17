"""Knowledge module: read-only BookStack adapter (server side).

The browser never talks to BookStack and never sees the API token. This
module reads the token from a file named in config/sources.json
(`modules.knowledge.bookstack.token_file`, mode 600), calls the BookStack
REST API (`/api/search`, `/api/pages/{id}`, `/api/books/{id}`), and hands the
browser plain text plus a few fields.

Rules this module keeps:
- Zero LLM calls (SPEC.md constraint 1). Plain HTTP GETs only.
- No content cache. Every response is a live fetch; `fetched_at` says when.
  If BookStack is down the caller gets an error and the outage times, never
  an older copy shown as current.
- Degrade gracefully (SPEC.md constraint 4). Every failure becomes a
  KnowledgeError with a short code. Nothing here raises into the app.
- The token and the Authorization header are never logged and never put in a
  response body. Redirects are not followed, so the token cannot be sent to
  another host.
"""

from __future__ import annotations

import logging
import os
import stat
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import httpx

from .modules import module_config

logger = logging.getLogger("critdash.knowledge")

MAX_QUERY_CHARS = 200
SEARCH_COUNT = 20
PREVIEW_CHARS = 280
PAGE_TEXT_MAX_CHARS = 200_000
DEFAULT_TIMEOUT_S = 8.0

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "tr", "table", "blockquote", "pre",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr",
}
_SKIP_TAGS = {"script", "style"}


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class KnowledgeError(Exception):
    """code: not_configured | token_file_missing | token_file_permissions |
    token_file_invalid | unreachable | auth_failed | upstream_error |
    not_found. http_status is the status BookStack returned, if any."""

    def __init__(self, code: str, message: str, http_status: int | None = None):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """BookStack HTML -> plain text. The browser only ever gets text, so no
    page markup can run script in the dashboard origin."""
    parser = _TextExtractor()
    parser.feed(html or "")
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.parts).split("\n"))
    return "\n".join(line for line in lines if line)


def read_token(path_str: str) -> str:
    """'id:secret' from a mode-600 file. Raises KnowledgeError."""
    if not path_str:
        raise KnowledgeError("not_configured", "modules.knowledge.bookstack.token_file is not set")
    path = Path(os.path.expanduser(path_str))
    try:
        st = path.stat()
    except OSError:
        raise KnowledgeError("token_file_missing", "token file not found or not readable") from None
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KnowledgeError(
            "token_file_permissions", "token file must not be readable by group or others (chmod 600)"
        )
    try:
        raw = path.read_text().strip()
    except OSError:
        raise KnowledgeError("token_file_missing", "token file not found or not readable") from None
    token_id, sep, secret = raw.partition(":")
    if not sep or not token_id or not secret or "\n" in raw:
        raise KnowledgeError("token_file_invalid", "token file must hold one line: <token_id>:<token_secret>")
    return raw


class BookStackAdapter:
    def __init__(self, sources: dict, transport: httpx.AsyncBaseTransport | None = None):
        # `sources` is Config.sources, the live dict; re-read on every call so
        # an edit to sources.json (after reload_sources) takes effect.
        self._sources = sources
        self._transport = transport
        self.last_success: str | None = None
        self.down_since: str | None = None
        self.last_error: dict | None = None

    # ---- config ----
    def _settings(self) -> tuple[str, float]:
        cfg = module_config(self._sources, "knowledge")
        if cfg is None:
            raise KnowledgeError("not_configured", "knowledge module is disabled")
        bs = cfg.get("bookstack")
        base = bs.get("base_url") if isinstance(bs, dict) else None
        if not isinstance(base, str) or not base.startswith(("http://", "https://")):
            raise KnowledgeError(
                "not_configured", "modules.knowledge.bookstack.base_url must be an http(s) URL"
            )
        try:
            timeout = float(bs.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        return base.rstrip("/"), min(max(timeout, 1.0), 30.0)

    def _token_path(self) -> str:
        bs = (module_config(self._sources, "knowledge") or {}).get("bookstack") or {}
        return str(bs.get("token_file") or "")

    # ---- health ----
    def _record_ok(self) -> None:
        self.last_success = now_iso()
        self.down_since = None
        self.last_error = None

    def _record_failure(self, err: KnowledgeError) -> None:
        if err.code in ("not_found",):
            # BookStack answered; the page is just missing. Not an outage.
            self._record_ok()
            return
        if self.down_since is None:
            self.down_since = now_iso()
        self.last_error = {"code": err.code, "message": err.message, "http_status": err.http_status}

    def status(self) -> dict:
        return {
            "ok": self.down_since is None and self.last_success is not None,
            "last_success": self.last_success,
            "down_since": self.down_since,
            "error": self.last_error,
        }

    # ---- HTTP ----
    async def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            base, timeout = self._settings()
            token = read_token(self._token_path())
            async with httpx.AsyncClient(
                transport=self._transport, timeout=timeout, follow_redirects=False
            ) as client:
                resp = await client.get(
                    base + path, params=params,
                    headers={"Authorization": f"Token {token}", "Accept": "application/json"},
                )
            if resp.status_code in (401, 403):
                raise KnowledgeError(
                    "auth_failed", f"BookStack rejected the configured token (HTTP {resp.status_code})",
                    resp.status_code,
                )
            if resp.status_code == 404:
                raise KnowledgeError("not_found", "not found in BookStack", 404)
            if resp.status_code != 200:
                raise KnowledgeError(
                    "upstream_error", f"BookStack returned HTTP {resp.status_code}", resp.status_code
                )
            try:
                body = resp.json()
            except ValueError:
                raise KnowledgeError("upstream_error", "BookStack returned a non-JSON body", 200) from None
            if not isinstance(body, dict):
                raise KnowledgeError("upstream_error", "BookStack returned an unexpected body", 200)
        except KnowledgeError as err:
            # Config/token-file problems are the operator's to fix, not an
            # outage of BookStack: do not start an outage clock for them.
            if err.code in ("not_configured", "token_file_missing", "token_file_permissions",
                            "token_file_invalid"):
                self.last_error = {"code": err.code, "message": err.message, "http_status": None}
            else:
                self._record_failure(err)
            raise
        except httpx.HTTPError as exc:
            # Type name only: never log exc text, request headers or the token.
            logger.warning("bookstack request failed: %s", type(exc).__name__)
            err = KnowledgeError("unreachable", "BookStack did not respond")
            self._record_failure(err)
            raise err from None
        self._record_ok()
        return body

    # ---- operations ----
    async def probe(self) -> dict:
        await self._get("/api/books", {"count": 1})
        return self.status()

    async def search(self, query: str) -> dict:
        q = query.strip()
        if not q:
            return {"results": [], "total": 0}
        query = f"{q[:MAX_QUERY_CHARS]} {{type:page}}"
        body = await self._get("/api/search", {"query": query, "count": SEARCH_COUNT})
        results = []
        for item in body.get("data") or []:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            if not isinstance(item.get("id"), int):
                continue
            book = item.get("book") if isinstance(item.get("book"), dict) else {}
            preview = item.get("preview_html") if isinstance(item.get("preview_html"), dict) else {}
            results.append({
                "id": item["id"],
                "name": str(item.get("name") or ""),
                "book": str(book.get("name") or ""),
                "updated_at": item.get("updated_at"),
                "preview": html_to_text(str(preview.get("content") or ""))[:PREVIEW_CHARS],
            })
        return {"results": results, "total": body.get("total", len(results))}

    async def page(self, page_id: int) -> dict:
        base, _ = self._settings()
        page = await self._get(f"/api/pages/{page_id}")
        book_id = page.get("book_id")
        book = await self._get(f"/api/books/{book_id}") if isinstance(book_id, int) else {}
        book_slug = book.get("slug") or page.get("book_slug")
        slug = page.get("slug")
        edit_url = None
        if book_slug and slug:
            edit_url = f"{base}/books/{quote(str(book_slug), safe='')}/page/{quote(str(slug), safe='')}/edit"
        text = html_to_text(str(page.get("html") or ""))
        truncated = len(text) > PAGE_TEXT_MAX_CHARS
        tags = [
            {"name": str(t.get("name") or ""), "value": str(t.get("value") or "")}
            for t in page.get("tags") or [] if isinstance(t, dict)
        ]
        return {
            "id": page.get("id", page_id),
            "name": str(page.get("name") or ""),
            "book": {"id": book_id, "name": str(book.get("name") or "")},
            "text": text[:PAGE_TEXT_MAX_CHARS],
            "truncated": truncated,
            "updated_at": page.get("updated_at"),
            "provenance": {
                "tags": tags,
                "created_by": _person(page.get("created_by")),
                "updated_by": _person(page.get("updated_by")),
                "revision_count": page.get("revision_count"),
            },
            "edit_url": edit_url,
            "open_url": f"{base}/link/{page_id}",
        }


def _person(obj) -> str | None:
    return str(obj.get("name")) if isinstance(obj, dict) and obj.get("name") else None
