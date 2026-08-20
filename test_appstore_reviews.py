"""Offline unit tests for appstore_reviews.py (no network calls)."""

from __future__ import annotations

import ast
import csv
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from appstore_reviews import (
    APP_STORE_COUNTRY_CODES,
    AppStoreReviewsError,
    CheckpointStore,
    InvalidAppURL,
    PageResult,
    Review,
    RunStats,
    collect_reviews,
    export_csv,
    extract_app_id,
    extract_xml_review_text,
    get_country_codes,
    make_page_fingerprint,
    parse_json_feed,
    parse_xml_feed,
    run,
)


def sample_feed() -> dict:
    return {
        "feed": {
            "entry": [
                {
                    "id": {"label": "app-service-entry"},
                    "im:name": {"label": "Example App"},
                    "summary": {"label": "Application metadata, not a review"},
                },
                {
                    "id": {"label": "review-101"},
                    "updated": {"label": "2026-08-17T12:34:56-07:00"},
                    "im:rating": {"label": "5"},
                    "title": {"label": "A title that must not be exported"},
                    "content": {"label": "Текст, с запятой, \"кавычками\"\r\nи эмодзи 🚀"},
                },
            ],
            "link": [
                {
                    "attributes": {
                        "rel": "next",
                        "href": "https://itunes.apple.com/us/rss/customerreviews/page=2/id=123/json",
                    }
                }
            ],
        }
    }


class ExtractAppIdTests(unittest.TestCase):
    def test_bare_numeric_id(self) -> None:
        self.assertEqual(extract_app_id("570060128"), "570060128")
        self.assertEqual(extract_app_id("  570060128  "), "570060128")

    def test_multiple_valid_url_shapes(self) -> None:
        cases = {
            "https://apps.apple.com/us/app/example/id123456789": "123456789",
            "https://apps.apple.com/de/app/name/id987654321?l=en&mt=8": "987654321",
            "https://apps.apple.com/app/id42": "42",
            "apps.apple.com/ru/app/a/b/id555/extra?x=1": "555",
            "https://itunes.apple.com/us/app/example/id777": "777",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(extract_app_id(url), expected)

    def test_invalid_urls_are_rejected(self) -> None:
        invalid = [
            "",
            "https://example.com/us/app/example/id123",
            "https://apps.apple.com/us/app/example",
            "https://apps.apple.com/us/app/example/idABC",
            "0",
            "-570060128",
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(InvalidAppURL):
                extract_app_id(url)


class CountryCodeTests(unittest.TestCase):
    def test_all_regions_are_the_175_unique_app_storefronts(self) -> None:
        codes = get_country_codes(None)
        self.assertEqual(codes, list(APP_STORE_COUNTRY_CODES))
        self.assertEqual(len(codes), 175)
        self.assertEqual(len(set(codes)), 175)
        self.assertTrue(all(len(code) == 2 and code.islower() for code in codes))
        self.assertIn("xk", codes)
        self.assertIn("ne", codes)
        self.assertIn("ng", codes)

    def test_explicit_storefront_codes_are_normalized_and_deduplicated(self) -> None:
        self.assertEqual(get_country_codes("US, xk, us"), ["us", "xk"])

    def test_unsupported_iso_country_is_rejected(self) -> None:
        with self.assertRaises(AppStoreReviewsError):
            get_country_codes("aq")


class ParsingTests(unittest.TestCase):
    def test_json_parse_skips_service_entry(self) -> None:
        page = parse_json_feed(sample_feed(), "us", 1)
        self.assertEqual(len(page.reviews), 1)

    def test_json_extracts_date_rating_and_review_content(self) -> None:
        page = parse_json_feed(sample_feed(), "us", 1)
        review = page.reviews[0]
        self.assertEqual(review.review_id, "review-101")
        self.assertEqual(review.date, "2026-08-17")
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.text, "Текст, с запятой, \"кавычками\"\nи эмодзи 🚀")
        self.assertIn("page=2", page.next_url or "")

    def test_xml_fallback_parser(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom"
              xmlns:im="http://itunes.apple.com/rss">
          <link rel="next" href="https://itunes.apple.com/us/rss/customerreviews/page=2/id=123/xml" />
          <entry><id>application</id><title>Metadata</title></entry>
          <entry><id>xml-review</id><updated>2026-08-18T10:00:00Z</updated>
            <content>XML text</content><im:rating>3</im:rating></entry>
        </feed>"""
        page = parse_xml_feed(xml, "us", 1)
        self.assertEqual(page.reviews, (Review("2026-08-18", 3, "XML text", "xml-review"),))
        self.assertIn("page=2", page.next_url or "")

    def test_xml_html_wrapper_exports_only_review_body(self) -> None:
        html = """<table><tr><td><b><a>Review title</a></b>
        <font></font></td></tr><tr><td><font><br/>Actual review 🚀</font></td></tr></table>"""
        self.assertEqual(extract_xml_review_text(html), "Actual review 🚀")

    def test_page_fingerprint_detects_repetition(self) -> None:
        reviews = (
            Review("2026-01-01", 5, "one", "id-1"),
            Review("2026-01-02", 4, "two", "id-2"),
        )
        self.assertEqual(make_page_fingerprint(reviews), make_page_fingerprint(reviews))
        self.assertNotEqual(make_page_fingerprint(reviews), make_page_fingerprint(tuple(reversed(reviews))))


class CheckpointTests(unittest.TestCase):
    def test_dedup_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with CheckpointStore(Path(directory) / "checkpoint.sqlite3") as store:
                store.bind_app("123")
                store.ensure_regions("123", ["us"])
                first = Review("2026-01-01", 5, "same text", "stable-id")
                same_id_changed_text = Review("2026-01-02", 1, "changed", "stable-id")
                result = PageResult(
                    reviews=(first, same_id_changed_text),
                    next_url=None,
                    feed_format="json",
                    fingerprint=make_page_fingerprint((first, same_id_changed_text)),
                )
                new_count, duplicate_count = store.save_page(
                    "123", "us", 1, result, next_page=2, next_url=None
                )
                self.assertEqual((new_count, duplicate_count), (1, 1))
                self.assertEqual(store.unique_count("123"), 1)

    def test_dedup_by_fallback_hash(self) -> None:
        first = Review("2026-01-03", 4, "fallback", None)
        same = Review("2026-01-03", 4, "fallback", None)
        different_date = Review("2026-01-04", 4, "fallback", None)
        self.assertEqual(first.dedup_key, same.dedup_key)
        self.assertNotEqual(first.dedup_key, different_date.dedup_key)

    def test_repeated_page_and_resume_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoint.sqlite3"
            review = Review("2026-01-01", 5, "resume", "r-1")
            result = PageResult(
                reviews=(review,),
                next_url="https://itunes.apple.com/us/next",
                feed_format="json",
                fingerprint=make_page_fingerprint((review,)),
            )
            with CheckpointStore(checkpoint) as store:
                store.bind_app("123")
                store.ensure_regions("123", ["us"])
                store.save_page("123", "us", 1, result, next_page=2, next_url=result.next_url)
                self.assertTrue(store.is_repeated_fingerprint("123", "us", result.fingerprint))
            with CheckpointStore(checkpoint) as reopened:
                reopened.bind_app("123")
                region = reopened.region("123", "us")
                self.assertEqual(region["next_page"], 2)
                self.assertEqual(region["next_url"], result.next_url)
                self.assertEqual(reopened.unique_count("123"), 1)

    def test_csv_has_exact_columns_and_round_trips_special_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / "reviews.csv"
            text = "Кириллица, \"кавычки\"\nвторая строка 😀"
            review = Review("2026-02-03", 4, text, "r-csv")
            result = PageResult(
                reviews=(review,),
                next_url=None,
                feed_format="json",
                fingerprint=make_page_fingerprint((review,)),
            )
            with CheckpointStore(base / "checkpoint.sqlite3") as store:
                store.bind_app("123")
                store.ensure_regions("123", ["ru"])
                store.save_page("123", "ru", 1, result, next_page=2, next_url=None)
                export_csv(store, "123", output)
            with output.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter=";"))
            self.assertEqual(list(rows[0]), ["Дата", "Оценка", "Текст"])
            self.assertEqual(
                rows[0],
                {"Дата": "03.02.2026", "Оценка": "★★★★", "Текст": text},
            )


class ProgressAndEmbeddingTests(unittest.TestCase):
    def test_collect_reviews_reports_structured_progress(self) -> None:
        class SinglePageClient:
            temporary_errors = 0
            permanent_errors = 0

            def fetch_page(self, app_id, country, page, next_url):
                review = Review("2026-08-20", 5, "Progress", "progress-1")
                return PageResult(
                    reviews=(review,),
                    next_url=None,
                    feed_format="json",
                    fingerprint=make_page_fingerprint((review,)),
                )

        events: list[tuple[int, int, str, int, RunStats]] = []
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with CheckpointStore(base / "checkpoint.sqlite3") as store:
                store.bind_app("123")
                stats = collect_reviews(
                    "123",
                    ["us"],
                    store,
                    SinglePageClient(),
                    base / "reviews.csv",
                    progress_callback=lambda *event: events.append(event),
                )

        self.assertGreaterEqual(len(events), 3)
        self.assertTrue(all(event[:4] == (1, 1, "us", 1) for event in events))
        self.assertEqual(events[-1][4].unique_reviews, 1)
        self.assertEqual(stats.unique_reviews, 1)

    def test_run_reset_is_noninteractive_and_forwards_callback(self) -> None:
        callback = lambda *args: None
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with (
                patch("appstore_reviews._confirm_reset") as confirm,
                patch("appstore_reviews.get_country_codes", return_value=["us"]),
                patch("appstore_reviews.AppleReviewsClient") as client_class,
                patch(
                    "appstore_reviews.collect_reviews",
                    return_value=RunStats(app_id="123"),
                ) as collect,
            ):
                client_class.return_value.__enter__.return_value = object()
                run(
                    "123",
                    output=str(base / "reviews.csv"),
                    checkpoint=str(base / "checkpoint.sqlite3"),
                    countries=["us"],
                    reset=True,
                    progress_callback=callback,
                )

        confirm.assert_not_called()
        self.assertIs(collect.call_args.kwargs["progress_callback"], callback)


class StreamlitSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = Path(__file__).with_name("streamlit_app.py").read_text(
            encoding="utf-8"
        )
        cls.tree = ast.parse(cls.source)

    def test_log_handler_is_filtered_by_current_thread(self) -> None:
        self.assertIn(
            "handler.addFilter(SessionThreadFilter(threading.get_ident()))",
            self.source,
        )

    def test_validated_app_id_is_passed_to_run(self) -> None:
        run_calls = [
            node
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run"
        ]
        self.assertEqual(len(run_calls), 1)
        self.assertIsInstance(run_calls[0].args[0], ast.Name)
        self.assertEqual(run_calls[0].args[0].id, "app_id")

    def test_collection_uses_rerun_state_machine(self) -> None:
        collecting_position = self.source.index(
            "st.session_state.collecting = True"
        )
        first_rerun_position = self.source.index("st.rerun()", collecting_position)
        execution_position = self.source.index(
            "if st.session_state.collecting and st.session_state.pending_job"
        )
        self.assertLess(collecting_position, first_rerun_position)
        self.assertLess(first_rerun_position, execution_position)

    def test_saved_checkpoint_is_bound_to_its_app_id(self) -> None:
        self.assertIn("st.session_state.checkpoint_app_id = app_id", self.source)
        self.assertIn("saved_checkpoint_app_id == app_id", self.source)
        self.assertIn('"checkpoint_reset": checkpoint_reset', self.source)

    def test_checkpoint_upload_limit_is_explicit(self) -> None:
        config_path = Path(__file__).with_name(".streamlit") / "config.toml"
        with config_path.open("rb") as handle:
            config = tomllib.load(handle)
        self.assertEqual(config["server"]["maxUploadSize"], 200)


if __name__ == "__main__":
    unittest.main()
