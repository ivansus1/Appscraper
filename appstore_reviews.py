#!/usr/bin/env python3
"""Collect the maximum reviews exposed by Apple's public customer-review RSS.

Accept either an App Store URL or a bare numeric app ID. The collector is
intentionally sequential, uses no API key, and stores every
successfully parsed page in SQLite before continuing.  The public feed is
usually limited to at most 10 pages (roughly 500 recent reviews) per storefront;
Apple does not promise completeness, stability, or a documented rate limit.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import random
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # Keep offline parser/SQLite tests importable before installation.
    requests = None  # type: ignore[assignment]

LOGGER = logging.getLogger("appstore_reviews")
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_CHECKPOINT = "appstore_reviews_checkpoint.sqlite3"
DEFAULT_OUTPUT = "appstore_reviews.csv"
PUBLIC_FEED_WARNING = (
    "Собран максимум отзывов, доступных через использованный публичный "
    "интерфейс Apple на момент запуска. Полнота исторической выгрузки не "
    "гарантируется."
)
# ISO 3166-1 alpha-2 codes are embedded so the notebook does not depend on pycountry.
ISO_COUNTRY_CODES = tuple(
    """
    ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm bn bo bq br bs bt bv bw by bz
    ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr
    ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo
    jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mf mg mh mk ml mm mn mo mp mq mr
    ms mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro
    rs ru rw sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv
    tw tz ua ug um us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw
    """.split()
)


class AppStoreReviewsError(Exception):
    """Base exception for expected collector failures."""


class InvalidAppURL(AppStoreReviewsError):
    """Raised when an App Store ID cannot be extracted from a URL."""


class CheckpointMismatch(AppStoreReviewsError):
    """Raised when a checkpoint belongs to another app."""


class CheckpointCorrupt(AppStoreReviewsError):
    """Raised when SQLite cannot read or initialize the checkpoint."""


class FetchError(AppStoreReviewsError):
    """A failed HTTP request with retry classification."""

    def __init__(self, message: str, *, temporary: bool, status: int | None = None):
        super().__init__(message)
        self.temporary = temporary
        self.status = status


class FeedStructureError(AppStoreReviewsError):
    """Raised when Apple returns an unrecognized feed structure."""


@dataclass(frozen=True)
class Review:
    """Normalized review plus its internal deduplication identity."""

    date: str
    rating: int
    text: str
    review_id: str | None = None

    @property
    def dedup_key(self) -> str:
        if self.review_id:
            return f"id:{self.review_id.strip()}"
        normalized = "\x1f".join((self.date, str(self.rating), self.text))
        return "hash:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PageResult:
    """Parsed contents and pagination metadata for one feed page."""

    reviews: tuple[Review, ...]
    next_url: str | None
    feed_format: str
    fingerprint: str


@dataclass
class RunStats:
    """Counters accumulated during one invocation."""

    app_id: str
    requested_countries: int = 0
    checked_countries: int = 0
    countries_with_reviews: int = 0
    countries_without_reviews: int = 0
    completed_regions: int = 0
    incomplete_regions: int = 0
    fetched_before_dedup: int = 0
    unique_reviews: int = 0
    duplicates: int = 0
    temporary_errors: int = 0
    permanent_errors: int = 0
    max_pages_regions: list[str] = field(default_factory=list)
    unfinished_regions: list[str] = field(default_factory=list)
    date_min: str | None = None
    date_max: str | None = None

    @property
    def errors(self) -> int:
        return self.temporary_errors + self.permanent_errors


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def extract_app_id(url: str) -> str:
    """Return an App Store ID from either a bare number or a valid Apple URL."""

    value = (url or "").strip()
    if not value:
        raise InvalidAppURL("Ссылка или ID App Store пусты.")
    if re.fullmatch(r"[1-9]\d*", value):
        return value
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in {"apps.apple.com", "itunes.apple.com"}:
        raise InvalidAppURL("Ожидалась ссылка с домена apps.apple.com.")
    match = re.search(r"(?:^|/)id(\d+)(?:/|$)", parsed.path, flags=re.IGNORECASE)
    if not match:
        raise InvalidAppURL("В ссылке не найден числовой идентификатор вида id123456789.")
    return match.group(1)


def get_country_codes(country_argument: str | Sequence[str] | None = None) -> list[str]:
    """Return validated lower-case country codes or every embedded ISO code."""

    known = set(ISO_COUNTRY_CODES)
    if country_argument is None:
        return sorted(known)
    if isinstance(country_argument, str):
        raw_codes = country_argument.split(",")
    else:
        raw_codes = list(country_argument)
    codes = [str(code).strip().lower() for code in raw_codes if str(code).strip()]
    if not codes:
        raise AppStoreReviewsError("--countries не содержит кодов стран.")
    invalid = sorted(set(codes) - known)
    if invalid:
        raise AppStoreReviewsError(
            "Некорректные ISO 3166-1 alpha-2 коды: " + ", ".join(invalid)
        )
    return list(dict.fromkeys(codes))


def _label(value: Any) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get("label")
        return str(candidate) if candidate is not None else None
    return str(value) if value is not None else None


def normalize_text(text: str) -> str:
    """Normalize transport line endings without changing review language/content."""

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_date(value: str) -> str:
    """Convert an ISO-like feed timestamp to YYYY-MM-DD."""

    candidate = value.strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", candidate)
    if not match:
        raise ValueError(f"неожиданная дата: {value!r}")
    datetime.strptime(match.group(1), "%Y-%m-%d")
    return match.group(1)


def make_page_fingerprint(reviews: Iterable[Review]) -> str:
    """Build a stable fingerprint for detecting repeated pages."""

    keys = [review.dedup_key for review in reviews]
    if not keys:
        return ""
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def _extract_next_from_json(feed: Mapping[str, Any]) -> str | None:
    links = feed.get("link", [])
    if isinstance(links, Mapping):
        links = [links]
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, Mapping):
            continue
        attrs = link.get("attributes", {})
        if isinstance(attrs, Mapping) and str(attrs.get("rel", "")).lower() == "next":
            href = str(attrs.get("href", "")).strip()
            return href or None
    return None


def parse_json_feed(payload: Any, country: str = "", page: int = 1) -> PageResult:
    """Parse Apple's JSON customer-review feed and ignore its app metadata entry."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("feed"), Mapping):
        raise FeedStructureError("JSON не содержит объект feed.")
    feed = payload["feed"]
    entries = feed.get("entry", [])
    if isinstance(entries, Mapping):
        entries = [entries]
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise FeedStructureError("feed.entry имеет неожиданный тип.")

    reviews: list[Review] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            LOGGER.warning("%s page %s: запись %s не является объектом", country, page, index)
            continue
        rating_text = _label(entry.get("im:rating"))
        if rating_text is None:
            continue  # Application/service entry, not a customer review.
        try:
            rating = int(rating_text)
            if not 1 <= rating <= 5:
                raise ValueError("rating outside 1..5")
            date = normalize_date(_label(entry.get("updated")) or "")
            text = normalize_text(_label(entry.get("content")) or "")
            if not text:
                raise ValueError("empty review text")
            review_id = (_label(entry.get("id")) or "").strip() or None
        except (TypeError, ValueError) as exc:
            LOGGER.warning("%s page %s: пропущена поврежденная запись: %s", country, page, exc)
            continue
        reviews.append(Review(date=date, rating=rating, text=text, review_id=review_id))

    result = tuple(reviews)
    return PageResult(
        reviews=result,
        next_url=_extract_next_from_json(feed),
        feed_format="json",
        fingerprint=make_page_fingerprint(result),
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_xml_feed(payload: bytes | str, country: str = "", page: int = 1) -> PageResult:
    """Parse Apple's XML/Atom fallback customer-review feed."""

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise FeedStructureError(f"невалидный XML: {exc}") from exc
    if _local_name(root.tag) != "feed":
        raise FeedStructureError("XML-корень не является Atom feed.")

    next_url: str | None = None
    for child in root:
        if _local_name(child.tag) == "link" and child.attrib.get("rel", "").lower() == "next":
            next_url = child.attrib.get("href", "").strip() or None
            break

    reviews: list[Review] = []
    for index, entry in enumerate(child for child in root if _local_name(child.tag) == "entry"):
        values: dict[str, list[str]] = {}
        for child in entry:
            values.setdefault(_local_name(child.tag), []).append(child.text or "")
        if "rating" not in values:
            continue
        try:
            rating = int(values["rating"][0].strip())
            if not 1 <= rating <= 5:
                raise ValueError("rating outside 1..5")
            date = normalize_date(values.get("updated", [""])[0])
            contents = values.get("content", [])
            text = normalize_text(contents[-1] if contents else "")
            if not text:
                raise ValueError("empty review text")
            review_id = values.get("id", [""])[0].strip() or None
        except (TypeError, ValueError) as exc:
            LOGGER.warning("%s page %s: пропущена поврежденная XML-запись %s: %s", country, page, index, exc)
            continue
        reviews.append(Review(date=date, rating=rating, text=text, review_id=review_id))

    result = tuple(reviews)
    return PageResult(
        reviews=result,
        next_url=next_url,
        feed_format="xml",
        fingerprint=make_page_fingerprint(result),
    )


def safe_structure_sample(payload: Any, limit: int = 600) -> str:
    """Return keys/types only, avoiding storage of review bodies."""

    def describe(value: Any, depth: int = 0) -> Any:
        if depth >= 3:
            return type(value).__name__
        if isinstance(value, Mapping):
            return {str(key): describe(item, depth + 1) for key, item in list(value.items())[:12]}
        if isinstance(value, list):
            return {"type": "list", "length": len(value), "first": describe(value[0], depth + 1) if value else None}
        return type(value).__name__

    return json.dumps(describe(payload), ensure_ascii=False)[:limit]


class AppleReviewsClient:
    """Adapter around Apple's replaceable public RSS customer-review source."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 4,
        delay: float = 1.0,
        backoff_base: float = 1.5,
        jitter: float = 0.4,
        session: Any | None = None,
    ) -> None:
        if requests is None:
            raise AppStoreReviewsError(
                "Не установлена зависимость requests. Выполните: pip install requests"
            )
        self.timeout = timeout
        self.retries = retries
        self.delay = delay
        self.backoff_base = backoff_base
        self.jitter = jitter
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "AppStoreReviewsPublicRSS/1.0 (+ordinary Python requests client)"}
        )
        self.temporary_errors = 0
        self.permanent_errors = 0
        self._last_request_at: float | None = None

    def __enter__(self) -> "AppleReviewsClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        self.session.close()

    @staticmethod
    def build_url(app_id: str, country: str, page: int, feed_format: str = "json") -> str:
        return (
            f"https://itunes.apple.com/{country.lower()}/rss/customerreviews/"
            f"page={page}/id={app_id}/sortby=mostrecent/{feed_format}"
        )

    def _wait_between_requests(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _request(self, url: str) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait_between_requests()
            try:
                response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
                self._last_request_at = time.monotonic()
            except (requests.Timeout, requests.ConnectionError) as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
                self.temporary_errors += 1
                if attempt >= self.retries:
                    raise FetchError(f"сетевая ошибка после повторов: {exc}", temporary=True) from exc
                sleep_for = self.backoff_base * (2**attempt) + random.uniform(0, self.jitter)
                LOGGER.warning("Временная сетевая ошибка; повтор через %.1f с", sleep_for)
                time.sleep(sleep_for)
                continue

            status = response.status_code
            if 200 <= status < 300:
                return response
            if status in TRANSIENT_HTTP_STATUSES:
                self.temporary_errors += 1
                if attempt >= self.retries:
                    raise FetchError(
                        f"HTTP {status} после {self.retries + 1} попыток",
                        temporary=True,
                        status=status,
                    )
                retry_after = self._retry_after_seconds(response.headers.get("Retry-After"))
                sleep_for = retry_after if retry_after is not None else self.backoff_base * (2**attempt)
                sleep_for += random.uniform(0, self.jitter)
                LOGGER.warning("HTTP %s; повтор через %.1f с", status, sleep_for)
                time.sleep(sleep_for)
                continue

            self.permanent_errors += 1
            raise FetchError(f"постоянная ошибка HTTP {status}", temporary=False, status=status)
        raise FetchError(f"не удалось выполнить запрос: {last_error}", temporary=True)

    def fetch_page(
        self,
        app_id: str,
        country: str,
        page: int,
        next_url: str | None = None,
    ) -> PageResult:
        """Fetch JSON; fall back to XML when a successful JSON response is unusable."""

        json_url = next_url or self.build_url(app_id, country, page, "json")
        parsed_url = urlparse(json_url)
        if parsed_url.hostname not in {"itunes.apple.com", "itunes.com"}:
            raise FeedStructureError("feed.next указывает на неожиданный домен.")
        if parsed_url.path.rstrip("/").lower().endswith("/xml"):
            xml_response = self._request(json_url)
            return parse_xml_feed(xml_response.content, country, page)
        response = self._request(json_url)
        try:
            payload = response.json()
            return parse_json_feed(payload, country, page)
        except (ValueError, FeedStructureError) as json_error:
            sample: str
            try:
                sample = safe_structure_sample(response.json())
            except ValueError:
                sample = f"content-type={response.headers.get('Content-Type')!r}, bytes={len(response.content)}"
            LOGGER.warning(
                "%s page %s: JSON feed не распознан (%s); структура: %s; пробую XML",
                country,
                page,
                json_error,
                sample,
            )
            xml_url = self.build_url(app_id, country, page, "xml")
            xml_response = self._request(xml_url)
            return parse_xml_feed(xml_response.content, country, page)


class CheckpointStore:
    """Transactional SQLite checkpoint and deduplication store."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(self.path), timeout=30)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self._create_schema()
            result = self.connection.execute("PRAGMA integrity_check").fetchone()
            if result is None or result[0] != "ok":
                raise CheckpointCorrupt(f"SQLite integrity_check: {result[0] if result else 'no result'}")
        except (sqlite3.DatabaseError, OSError) as exc:
            raise CheckpointCorrupt(f"Не удалось открыть checkpoint {self.path}: {exc}") from exc

    def __enter__(self) -> "CheckpointStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    app_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    review_id TEXT,
                    country TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    rating INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (app_id, dedup_key)
                );
                CREATE TABLE IF NOT EXISTS regions (
                    app_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    next_page INTEGER NOT NULL DEFAULT 1,
                    next_url TEXT,
                    pages_done INTEGER NOT NULL DEFAULT 0,
                    reviews_found INTEGER NOT NULL DEFAULT 0,
                    hit_max_pages INTEGER NOT NULL DEFAULT 0,
                    last_attempt TEXT,
                    last_error TEXT,
                    PRIMARY KEY (app_id, country)
                );
                CREATE TABLE IF NOT EXISTS pages (
                    app_id TEXT NOT NULL,
                    country TEXT NOT NULL,
                    page INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    feed_format TEXT,
                    fingerprint TEXT,
                    raw_count INTEGER NOT NULL DEFAULT 0,
                    new_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    attempted_at TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY (app_id, country, page)
                );
                CREATE TABLE IF NOT EXISTS errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    country TEXT,
                    page INTEGER,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_reviews_export
                    ON reviews(app_id, date, rating);
                """
            )

    def bind_app(self, app_id: str) -> None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'app_id'"
        ).fetchone()
        if row and row[0] != app_id:
            raise CheckpointMismatch(
                f"Checkpoint относится к приложению {row[0]}, а ссылка — к {app_id}. "
                "Укажите другой --checkpoint или используйте --reset с подтверждением."
            )
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) VALUES('app_id', ?)", (app_id,)
            )

    def reset(self) -> None:
        with self.connection:
            self.connection.execute("DELETE FROM errors")
            self.connection.execute("DELETE FROM pages")
            self.connection.execute("DELETE FROM regions")
            self.connection.execute("DELETE FROM reviews")
            self.connection.execute("DELETE FROM metadata")

    def ensure_regions(self, app_id: str, countries: Sequence[str]) -> None:
        with self.connection:
            self.connection.executemany(
                "INSERT OR IGNORE INTO regions(app_id, country) VALUES(?, ?)",
                ((app_id, country) for country in countries),
            )

    def region(self, app_id: str, country: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM regions WHERE app_id = ? AND country = ?", (app_id, country)
        ).fetchone()
        if row is None:
            raise CheckpointCorrupt(f"Отсутствует строка региона {country}")
        return row

    def is_repeated_fingerprint(self, app_id: str, country: str, fingerprint: str) -> bool:
        if not fingerprint:
            return False
        row = self.connection.execute(
            """SELECT 1 FROM pages
               WHERE app_id = ? AND country = ? AND status = 'done' AND fingerprint = ?
               LIMIT 1""",
            (app_id, country, fingerprint),
        ).fetchone()
        return row is not None

    def save_page(
        self,
        app_id: str,
        country: str,
        page: int,
        result: PageResult,
        *,
        next_page: int,
        next_url: str | None,
    ) -> tuple[int, int]:
        """Save page and reviews atomically; return (new, duplicate) counts."""

        new_count = 0
        now = utc_now()
        try:
            with self.connection:
                for review in result.reviews:
                    cursor = self.connection.execute(
                        """INSERT OR IGNORE INTO reviews
                           (app_id, dedup_key, review_id, country, page, date, rating, text, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            app_id,
                            review.dedup_key,
                            review.review_id,
                            country,
                            page,
                            review.date,
                            review.rating,
                            review.text,
                            now,
                        ),
                    )
                    new_count += max(cursor.rowcount, 0)
                duplicate_count = len(result.reviews) - new_count
                self.connection.execute(
                    """INSERT INTO pages
                       (app_id, country, page, status, feed_format, fingerprint,
                        raw_count, new_count, duplicate_count, attempted_at, error)
                       VALUES (?, ?, ?, 'done', ?, ?, ?, ?, ?, ?, NULL)
                       ON CONFLICT(app_id, country, page) DO UPDATE SET
                         status='done', feed_format=excluded.feed_format,
                         fingerprint=excluded.fingerprint, raw_count=excluded.raw_count,
                         new_count=excluded.new_count, duplicate_count=excluded.duplicate_count,
                         attempted_at=excluded.attempted_at, error=NULL""",
                    (
                        app_id,
                        country,
                        page,
                        result.feed_format,
                        result.fingerprint,
                        len(result.reviews),
                        new_count,
                        duplicate_count,
                        now,
                    ),
                )
                self.connection.execute(
                    """UPDATE regions SET status='in_progress', next_page=?, next_url=?,
                       pages_done=pages_done+1, reviews_found=reviews_found+?,
                       last_attempt=?, last_error=NULL
                       WHERE app_id=? AND country=?""",
                    (next_page, next_url, new_count, now, app_id, country),
                )
        except sqlite3.DatabaseError as exc:
            raise CheckpointCorrupt(f"Ошибка сохранения страницы в SQLite: {exc}") from exc
        return new_count, duplicate_count

    def complete_region(
        self, app_id: str, country: str, status: str, *, hit_max_pages: bool = False
    ) -> None:
        with self.connection:
            self.connection.execute(
                """UPDATE regions SET status=?, hit_max_pages=?, last_attempt=?
                   WHERE app_id=? AND country=?""",
                (status, int(hit_max_pages), utc_now(), app_id, country),
            )

    def record_error(
        self,
        app_id: str,
        country: str | None,
        page: int | None,
        kind: str,
        message: str,
    ) -> None:
        clean_message = message[:1000]
        with self.connection:
            self.connection.execute(
                """INSERT INTO errors(app_id, country, page, kind, message, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (app_id, country, page, kind, clean_message, utc_now()),
            )
            if country:
                self.connection.execute(
                    """UPDATE regions SET status='error', last_attempt=?, last_error=?
                       WHERE app_id=? AND country=?""",
                    (utc_now(), clean_message, app_id, country),
                )

    def iter_reviews(self, app_id: str) -> Iterator[sqlite3.Row]:
        yield from self.connection.execute(
            """SELECT date, rating, text FROM reviews
               WHERE app_id=? ORDER BY date ASC, rating ASC, dedup_key ASC""",
            (app_id,),
        )

    def unique_count(self, app_id: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM reviews WHERE app_id=?", (app_id,)
            ).fetchone()[0]
        )

    def aggregate_stats(self, app_id: str, requested_countries: int) -> RunStats:
        stats = RunStats(app_id=app_id, requested_countries=requested_countries)
        rows = self.connection.execute(
            "SELECT status, reviews_found, hit_max_pages, country FROM regions WHERE app_id=?",
            (app_id,),
        ).fetchall()
        stats.checked_countries = sum(row["status"] != "pending" for row in rows)
        raw_countries = {
            row[0]
            for row in self.connection.execute(
                """SELECT country FROM pages
                   WHERE app_id=? AND status='done' AND raw_count > 0
                   GROUP BY country""",
                (app_id,),
            )
        }
        stats.countries_with_reviews = len(raw_countries)
        stats.countries_without_reviews = sum(
            row["status"] in {"complete_empty", "unavailable"} for row in rows
        )
        complete_states = {"complete", "complete_empty", "unavailable", "repeated_page"}
        stats.completed_regions = sum(row["status"] in complete_states for row in rows)
        stats.incomplete_regions = len(rows) - stats.completed_regions
        stats.max_pages_regions = sorted(row["country"] for row in rows if row["hit_max_pages"])
        stats.unfinished_regions = sorted(
            row["country"] for row in rows if row["status"] not in complete_states
        )
        page_totals = self.connection.execute(
            """SELECT COALESCE(SUM(raw_count),0), COALESCE(SUM(new_count),0),
                      COALESCE(SUM(duplicate_count),0)
               FROM pages WHERE app_id=? AND status='done'""",
            (app_id,),
        ).fetchone()
        stats.fetched_before_dedup = int(page_totals[0])
        stats.unique_reviews = self.unique_count(app_id)
        stats.duplicates = int(page_totals[2])
        error_rows = self.connection.execute(
            "SELECT kind, COUNT(*) AS n FROM errors WHERE app_id=? GROUP BY kind", (app_id,)
        ).fetchall()
        for row in error_rows:
            if row["kind"] == "temporary":
                stats.temporary_errors += int(row["n"])
            else:
                stats.permanent_errors += int(row["n"])
        dates = self.connection.execute(
            "SELECT MIN(date), MAX(date) FROM reviews WHERE app_id=?", (app_id,)
        ).fetchone()
        stats.date_min, stats.date_max = dates[0], dates[1]
        return stats


def export_csv(store: CheckpointStore, app_id: str, output: str | Path) -> Path:
    """Export exactly date,rating,text using Excel-friendly UTF-8 BOM."""

    destination = Path(output).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "rating", "text"])
            for row in store.iter_reviews(app_id):
                writer.writerow([row["date"], row["rating"], row["text"]])
        temporary.replace(destination)
    except OSError as exc:
        raise AppStoreReviewsError(f"Не удалось записать CSV {destination}: {exc}") from exc
    return destination


def collect_reviews(
    app_id: str,
    countries: Sequence[str],
    store: CheckpointStore,
    client: AppleReviewsClient,
    output: str | Path,
    *,
    max_pages: int = 10,
    export_every_pages: int = 5,
) -> RunStats:
    """Collect sequentially, resuming every country from its checkpoint state."""

    if max_pages < 1:
        raise AppStoreReviewsError("--max-pages должен быть не меньше 1.")
    store.ensure_regions(app_id, countries)
    successful_pages_since_export = 0

    for country_index, country in enumerate(countries, start=1):
        region = store.region(app_id, country)
        if region["status"] in {"complete", "complete_empty", "unavailable", "repeated_page"}:
            LOGGER.info("[%s/%s] %s уже завершен; пропуск", country_index, len(countries), country)
            continue

        page = int(region["next_page"])
        next_url = region["next_url"]
        LOGGER.info("[%s/%s] регион %s, продолжение со страницы %s", country_index, len(countries), country, page)

        while page <= max_pages:
            try:
                result = client.fetch_page(app_id, country, page, next_url)
            except FetchError as exc:
                # A 404 commonly means no app/feed in this storefront. Other 4xx remain visible errors.
                if exc.status in {400, 404} and page == 1:
                    store.complete_region(app_id, country, "unavailable")
                    LOGGER.info("%s: приложение или feed недоступны (HTTP %s)", country, exc.status)
                elif exc.status == 404 and page > 1:
                    store.complete_region(app_id, country, "complete")
                    LOGGER.info("%s page %s: HTTP 404 трактуется как конец пагинации", country, page)
                else:
                    kind = "temporary" if exc.temporary else "permanent"
                    store.record_error(app_id, country, page, kind, str(exc))
                    LOGGER.error("%s page %s: %s", country, page, exc)
                break
            except FeedStructureError as exc:
                store.record_error(app_id, country, page, "permanent", str(exc))
                LOGGER.error("%s page %s: структура feed изменилась: %s", country, page, exc)
                break

            if store.is_repeated_fingerprint(app_id, country, result.fingerprint):
                store.complete_region(app_id, country, "repeated_page")
                LOGGER.warning("%s page %s полностью повторяет предыдущую страницу; регион остановлен", country, page)
                break

            new_count, duplicate_count = store.save_page(
                app_id,
                country,
                page,
                result,
                next_page=page + 1,
                next_url=result.next_url,
            )
            successful_pages_since_export += 1
            LOGGER.info(
                "%s page %s: найдено=%s, новых=%s, дублей=%s, временных ошибок=%s, постоянных=%s",
                country,
                page,
                len(result.reviews),
                new_count,
                duplicate_count,
                client.temporary_errors,
                client.permanent_errors,
            )

            if successful_pages_since_export >= max(1, export_every_pages):
                export_csv(store, app_id, output)
                successful_pages_since_export = 0

            if not result.reviews:
                store.complete_region(app_id, country, "complete_empty")
                break
            if not result.next_url:
                store.complete_region(app_id, country, "complete")
                break
            page += 1
            next_url = result.next_url

        else:
            store.complete_region(app_id, country, "max_pages", hit_max_pages=True)
            LOGGER.warning(
                "%s: достигнут --max-pages=%s; выгрузка региона может быть неполной",
                country,
                max_pages,
            )

        export_csv(store, app_id, output)

    return store.aggregate_stats(app_id, len(countries))


def print_summary(stats: RunStats, output: Path, checkpoint: Path) -> None:
    """Log the required human-readable final report."""

    LOGGER.info("\n%s", "=" * 68)
    LOGGER.info(PUBLIC_FEED_WARNING)
    LOGGER.info("ID приложения: %s", stats.app_id)
    LOGGER.info("Запрошено стран: %s", stats.requested_countries)
    LOGGER.info("Проверено стран: %s", stats.checked_countries)
    LOGGER.info("Стран с отзывами: %s", stats.countries_with_reviews)
    LOGGER.info("Стран без доступных отзывов: %s", stats.countries_without_reviews)
    LOGGER.info("Завершенных / незавершенных регионов: %s / %s", stats.completed_regions, stats.incomplete_regions)
    LOGGER.info("Получено до дедупликации: %s", stats.fetched_before_dedup)
    LOGGER.info("Уникальных отзывов: %s", stats.unique_reviews)
    LOGGER.info("Пропущено дублей: %s", stats.duplicates)
    LOGGER.info("Диапазон дат: %s — %s", stats.date_min or "нет данных", stats.date_max or "нет данных")
    LOGGER.info("Ошибок (временных / постоянных): %s (%s / %s)", stats.errors, stats.temporary_errors, stats.permanent_errors)
    LOGGER.info("CSV: %s", output)
    LOGGER.info("Checkpoint: %s", checkpoint)
    LOGGER.info("Лимит max_pages достигнут: %s", "да" if stats.max_pages_regions else "нет")
    if stats.max_pages_regions:
        LOGGER.warning("Регионы у лимита max_pages: %s", ", ".join(stats.max_pages_regions))
    if stats.unfinished_regions:
        LOGGER.warning("Незавершенные регионы для повторного запуска: %s", ", ".join(stats.unfinished_regions))


def _confirm_reset(path: Path) -> None:
    answer = input(
        f"Checkpoint {path} будет очищен. Для подтверждения введите СБРОСИТЬ: "
    ).strip()
    if answer != "СБРОСИТЬ":
        raise AppStoreReviewsError("Сброс отменен: точное подтверждение не получено.")


def run(
    app_url: str,
    *,
    output: str = DEFAULT_OUTPUT,
    checkpoint: str = DEFAULT_CHECKPOINT,
    countries: str | Sequence[str] | None = None,
    max_pages: int = 10,
    delay: float = 1.0,
    timeout: float = 20.0,
    retries: int = 4,
    reset: bool = False,
) -> RunStats:
    """Import-friendly high-level entry point."""

    app_id = extract_app_id(app_url)
    country_codes = get_country_codes(countries)
    output_path = Path(output).expanduser().resolve()
    checkpoint_path = Path(checkpoint).expanduser().resolve()
    if delay < 0 or timeout <= 0 or retries < 0:
        raise AppStoreReviewsError("delay/retries не могут быть отрицательными, timeout должен быть > 0.")

    with CheckpointStore(checkpoint_path) as store:
        if reset:
            _confirm_reset(checkpoint_path)
            store.reset()
        store.bind_app(app_id)
        try:
            with AppleReviewsClient(timeout=timeout, retries=retries, delay=delay) as client:
                stats = collect_reviews(
                    app_id,
                    country_codes,
                    store,
                    client,
                    output_path,
                    max_pages=max_pages,
                )
        except KeyboardInterrupt:
            LOGGER.warning("Остановка пользователем. Сохраняю накопленный CSV…")
            export_csv(store, app_id, output_path)
            raise
        finally:
            # A partial CSV remains usable even after a region-level or fatal error.
            export_csv(store, app_id, output_path)
        stats = store.aggregate_stats(app_id, len(country_codes))

    print_summary(stats, output_path, checkpoint_path)
    return stats


def is_notebook() -> bool:
    """Return True for IPython/Jupyter/Colab kernels without importing them."""

    return "ipykernel" in sys.modules or "google.colab" in sys.modules


def run_colab(app_url: str | None = None, **kwargs: Any) -> RunStats:
    """Colab/Jupyter helper; downloading remains an explicit separate action."""

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not app_url:
        app_url = input("Вставьте ссылку или числовой ID приложения в App Store: ").strip()
    stats = run(app_url, **kwargs)
    output = Path(str(kwargs.get("output", DEFAULT_OUTPUT))).expanduser().resolve()
    LOGGER.info("Файл готов: %s", output)
    if "google.colab" in sys.modules:
        LOGGER.info("Для явного скачивания выполните: download_colab_file(%r)", str(output))
    return stats


def download_colab_file(path: str = DEFAULT_OUTPUT) -> None:
    """Explicitly download one generated file in Google Colab."""

    try:
        from google.colab import files  # type: ignore
    except ImportError as exc:
        raise AppStoreReviewsError("Эта функция доступна только в Google Colab.") from exc
    files.download(str(Path(path).expanduser().resolve()))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "app_url",
        nargs="?",
        help="Ссылка на приложение в Apple App Store или числовой ID",
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Путь к итоговому CSV")
    parser.add_argument("--checkpoint", default=DEFAULT_CHECKPOINT, help="Путь к SQLite checkpoint")
    parser.add_argument("--countries", help="Коды через запятую, например us,gb,de,fr,ru")
    parser.add_argument("--max-pages", type=int, default=10, help="Максимум страниц на регион (по умолчанию: 10)")
    parser.add_argument("--delay", type=float, default=1.0, help="Минимальная пауза между запросами, сек")
    parser.add_argument("--timeout", type=float, default=20.0, help="Timeout одного запроса, сек")
    parser.add_argument("--retries", type=int, default=4, help="Повторы временных ошибок")
    parser.add_argument("--reset", action="store_true", help="Очистить checkpoint после текстового подтверждения")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Уровень логирования",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point that ignores Jupyter's service arguments."""

    effective_argv: Sequence[str] | None = [] if argv is None and is_notebook() else argv
    args = build_parser().parse_args(effective_argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    app_url = args.app_url or input(
        "Вставьте ссылку или числовой ID приложения в App Store: "
    ).strip()
    try:
        run(
            app_url,
            output=args.output,
            checkpoint=args.checkpoint,
            countries=args.countries,
            max_pages=args.max_pages,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            reset=args.reset,
        )
    except KeyboardInterrupt:
        LOGGER.warning("Работа остановлена пользователем; checkpoint и частичный CSV сохранены.")
        return 130
    except (AppStoreReviewsError, sqlite3.DatabaseError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
