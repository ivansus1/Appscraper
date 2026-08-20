"""Streamlit interface for the App Store review collector."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from appstore_reviews import (
    APP_STORE_COUNTRY_CODES,
    AppStoreReviewsError,
    CheckpointCorrupt,
    CheckpointMismatch,
    extract_app_id,
    run,
)


st.set_page_config(
    page_title="Отзывы App Store",
    page_icon="⭐",
    layout="centered",
)

WORK_DIR = Path("/tmp/appstore_reviews_streamlit")
STALE_FILE_TTL_SECONDS = 24 * 60 * 60
WORK_DIR.mkdir(parents=True, exist_ok=True)


def cleanup_stale_files() -> None:
    """Delete abandoned working files older than the configured TTL."""

    cutoff = time.time() - STALE_FILE_TTL_SECONDS
    for path in WORK_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Не удалось удалить устаревший временный файл %s: %s", path, exc
            )


cleanup_stale_files()


class SessionThreadFilter(logging.Filter):
    """Accept only log records emitted by the current Streamlit session thread."""

    def __init__(self, thread_id: int) -> None:
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread_id


class StreamlitLogHandler(logging.Handler):
    """Show the collector's latest log messages inside the Streamlit page."""

    def __init__(self, placeholder: object) -> None:
        super().__init__()
        self.placeholder = placeholder
        self.lines: list[str] = []
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
            self.lines = self.lines[-400:]
            self.placeholder.code("\n".join(self.lines), language="text")
        except Exception:
            self.handleError(record)


def save_available_files(
    csv_path: Path,
    checkpoint_path: Path,
    app_id: str,
    *,
    save_checkpoint: bool = True,
) -> None:
    """Keep generated files available across ordinary Streamlit reruns."""

    if csv_path.exists():
        st.session_state.csv_data = csv_path.read_bytes()
    if save_checkpoint and checkpoint_path.exists():
        st.session_state.checkpoint_data = checkpoint_path.read_bytes()
        st.session_state.checkpoint_app_id = app_id
    elif not save_checkpoint:
        st.session_state.checkpoint_data = None
        st.session_state.checkpoint_app_id = None


def delete_session_files(csv_path: Path, checkpoint_path: Path) -> None:
    """Remove one session's working files after their bytes are saved in memory."""

    for path in (
        csv_path,
        checkpoint_path,
        Path(f"{checkpoint_path}-wal"),
        Path(f"{checkpoint_path}-shm"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "Не удалось удалить временный файл сессии %s: %s", path, exc
            )


if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:12]
if "csv_data" not in st.session_state:
    st.session_state.csv_data = None
if "checkpoint_data" not in st.session_state:
    st.session_state.checkpoint_data = None
if "checkpoint_app_id" not in st.session_state:
    previous_summary = st.session_state.get("summary")
    st.session_state.checkpoint_app_id = (
        previous_summary.get("app_id") if isinstance(previous_summary, dict) else None
    )
if "summary" not in st.session_state:
    st.session_state.summary = None
if "last_log" not in st.session_state:
    st.session_state.last_log = ""
if "collecting" not in st.session_state:
    st.session_state.collecting = False
if "progress_snapshot" not in st.session_state:
    st.session_state.progress_snapshot = None
if "pending_job" not in st.session_state:
    st.session_state.pending_job = None
if "result_notice" not in st.session_state:
    st.session_state.result_notice = None


st.title("Сбор отзывов App Store")
st.write(
    "Введите числовой ID приложения. На выходе будет CSV только с колонками "
    "`Дата`, `Оценка`, `Текст`: дата в формате ДД.ММ.ГГГГ, "
    "оценка — от ★ до ★★★★★."
)

if st.session_state.result_notice is not None:
    notice_kind, notice_text = st.session_state.result_notice
    if notice_kind == "success":
        st.success(notice_text)
    else:
        st.error(notice_text)
    st.session_state.result_notice = None

# A form sends all parameters in one rerun instead of restarting the script
# whenever an individual widget changes.
with st.form("collector_form"):
    app_value = st.text_input(
        "ID приложения",
        placeholder="570060128",
    )
    all_regions = st.checkbox(
        f"Проверить все регионы App Store ({len(APP_STORE_COUNTRY_CODES)})",
        value=False,
    )
    countries = st.text_input(
        "Коды стран через запятую",
        value="us,gb,de,fr,ru",
        help="Если выбраны все регионы, это поле будет проигнорировано.",
    )

    left, right = st.columns(2)
    with left:
        max_pages = st.number_input(
            "Максимум страниц на регион",
            min_value=1,
            max_value=10,
            value=2,
            step=1,
        )
        delay = st.number_input(
            "Пауза между запросами, сек.",
            min_value=0.2,
            max_value=10.0,
            value=1.0,
            step=0.1,
        )
    with right:
        timeout = st.number_input(
            "Таймаут запроса, сек.",
            min_value=5,
            max_value=120,
            value=20,
            step=5,
        )
        retries = st.number_input(
            "Повторные попытки",
            min_value=0,
            max_value=10,
            value=4,
            step=1,
        )

    uploaded_checkpoint = st.file_uploader(
        "Checkpoint SQLite для продолжения — необязательно",
        type=["sqlite3", "sqlite", "db"],
        help="Загружайте только checkpoint от этого же приложения.",
    )
    start_fresh = st.checkbox(
        "Начать заново и удалить временный результат этой сессии",
        value=False,
    )
    submitted = st.form_submit_button(
        "Начать сбор",
        type="primary",
        use_container_width=True,
        disabled=st.session_state.collecting,
    )

if submitted:
    try:
        app_id = extract_app_id(app_value)
        if not app_value.strip().isdigit():
            raise AppStoreReviewsError("Введите только числовой ID приложения.")
        if not all_regions and not countries.strip():
            raise AppStoreReviewsError(
                "Введите хотя бы один код страны или выберите все регионы."
            )
        if start_fresh and uploaded_checkpoint is not None:
            raise AppStoreReviewsError(
                "Нельзя одновременно загрузить checkpoint и начать заново."
            )
    except AppStoreReviewsError as exc:
        st.error(str(exc))
    else:
        checkpoint_bytes = None
        checkpoint_reset = False
        if uploaded_checkpoint is not None:
            checkpoint_bytes = uploaded_checkpoint.getvalue()
        elif not start_fresh:
            saved_checkpoint = st.session_state.checkpoint_data
            saved_checkpoint_app_id = st.session_state.checkpoint_app_id
            if saved_checkpoint is not None and saved_checkpoint_app_id == app_id:
                checkpoint_bytes = saved_checkpoint
            elif saved_checkpoint is not None:
                checkpoint_reset = True

        st.session_state.pending_job = {
            "app_id": app_id,
            "all_regions": all_regions,
            "countries": countries,
            "max_pages": int(max_pages),
            "delay": float(delay),
            "timeout": float(timeout),
            "retries": int(retries),
            "start_fresh": start_fresh,
            "checkpoint_bytes": checkpoint_bytes,
            "checkpoint_reset": checkpoint_reset,
        }
        st.session_state.csv_data = None
        st.session_state.checkpoint_data = None
        st.session_state.checkpoint_app_id = None
        st.session_state.summary = None
        st.session_state.last_log = ""
        st.session_state.progress_snapshot = None
        st.session_state.collecting = True
        st.rerun()


if st.session_state.collecting and st.session_state.pending_job is not None:
    job = st.session_state.pending_job
    app_id = job["app_id"]
    session_id = st.session_state.session_id
    csv_path = WORK_DIR / f"reviews_{app_id}_{session_id}.csv"
    checkpoint_path = WORK_DIR / f"checkpoint_{app_id}_{session_id}.sqlite3"

    if job["all_regions"]:
        minimum_minutes = len(APP_STORE_COUNTRY_CODES) * job["delay"] / 60
        maximum_minutes = minimum_minutes * job["max_pages"]
        st.warning(
            f"Будет проверено {len(APP_STORE_COUNTRY_CODES)} регионов App Store. "
            f"Только обязательные паузы займут примерно "
            f"{minimum_minutes:.1f}–{maximum_minutes:.1f} мин.; "
            "сетевые запросы и повторные попытки увеличат это время."
        )
    if job["checkpoint_reset"]:
        st.info(
            "Сохранённый checkpoint относился к другому приложению и был "
            "автоматически сброшен."
        )

    progress_bar = st.progress(0.0, text="Подготовка к сбору…")
    progress_details = st.empty()
    log_placeholder = st.empty()
    status = st.status("Сбор отзывов выполняется…", expanded=True)

    def update_progress(country_index, total, country, page, stats):
        fraction = min(1.0, max(0.0, country_index / max(1, total)))
        label = (
            f"Регион {country_index}/{total}: {country.upper()}, страница {page}"
        )
        progress_bar.progress(fraction, text=label)
        progress_details.caption(
            f"Уникальных отзывов: {stats.unique_reviews} · "
            f"проверено регионов: {stats.checked_countries}/{total}"
        )
        st.session_state.progress_snapshot = {
            "country_index": country_index,
            "total": total,
            "country": country,
            "page": page,
            "unique_reviews": stats.unique_reviews,
        }

    handler = StreamlitLogHandler(log_placeholder)
    handler.setLevel(logging.INFO)
    handler.addFilter(SessionThreadFilter(threading.get_ident()))
    collector_logger = logging.getLogger("appstore_reviews")
    collector_logger.setLevel(logging.INFO)
    collector_logger.addHandler(handler)
    checkpoint_is_usable = True

    try:
        if job["start_fresh"]:
            delete_session_files(csv_path, checkpoint_path)
        if job["checkpoint_bytes"] is not None:
            checkpoint_path.write_bytes(job["checkpoint_bytes"])

        stats = run(
            app_id,
            output=str(csv_path),
            checkpoint=str(checkpoint_path),
            countries=None if job["all_regions"] else job["countries"],
            max_pages=job["max_pages"],
            delay=job["delay"],
            timeout=job["timeout"],
            retries=job["retries"],
            reset=False,
            progress_callback=update_progress,
        )
        st.session_state.summary = asdict(stats)
        st.session_state.result_notice = (
            "success",
            "CSV и checkpoint подготовлены.",
        )
        progress_bar.progress(1.0, text="Сбор завершён")
        status.update(label="Сбор завершён", state="complete", expanded=False)
    except (CheckpointMismatch, CheckpointCorrupt, sqlite3.DatabaseError) as exc:
        checkpoint_is_usable = False
        st.session_state.result_notice = ("error", str(exc))
        status.update(label="Сбор завершён с ошибкой", state="error")
    except (AppStoreReviewsError, OSError) as exc:
        st.session_state.result_notice = ("error", str(exc))
        status.update(label="Сбор завершён с ошибкой", state="error")
    except Exception as exc:
        st.session_state.result_notice = ("error", str(exc))
        status.update(label="Сбор завершён с ошибкой", state="error")
    finally:
        collector_logger.removeHandler(handler)
        handler.close()
        st.session_state.last_log = "\n".join(handler.lines)
        save_available_files(
            csv_path,
            checkpoint_path,
            app_id,
            save_checkpoint=checkpoint_is_usable,
        )
        delete_session_files(csv_path, checkpoint_path)
        st.session_state.pending_job = None
        st.session_state.collecting = False
    st.rerun()


if st.session_state.summary:
    summary = st.session_state.summary
    st.subheader("Сводка")
    col1, col2, col3 = st.columns(3)
    col1.metric("Уникальных отзывов", summary["unique_reviews"])
    col2.metric("Проверено стран", summary["checked_countries"])
    col3.metric("Ошибок", summary["temporary_errors"] + summary["permanent_errors"])
    st.write(
        f"Диапазон дат: {summary['date_min'] or '—'} — "
        f"{summary['date_max'] or '—'}"
    )

if st.session_state.csv_data is not None:
    st.download_button(
        "Скачать reviews.csv",
        data=st.session_state.csv_data,
        file_name="reviews.csv",
        mime="text/csv",
        use_container_width=True,
    )

if st.session_state.checkpoint_data is not None:
    st.download_button(
        "Скачать checkpoint.sqlite3",
        data=st.session_state.checkpoint_data,
        file_name="appstore_reviews_checkpoint.sqlite3",
        mime="application/octet-stream",
        help="Сохраните файл, чтобы продолжить после перезапуска приложения.",
        use_container_width=True,
    )

if st.session_state.last_log:
    with st.expander("Последний журнал выполнения"):
        st.code(st.session_state.last_log, language="text")

st.divider()
st.caption(
    "Локальные файлы Streamlit Community Cloud временные. "
    "После запуска скачайте CSV и checkpoint."
)
