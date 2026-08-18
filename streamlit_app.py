"""Streamlit interface for the App Store review collector."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path

import streamlit as st

from appstore_reviews import AppStoreReviewsError, extract_app_id, run


st.set_page_config(
    page_title="Отзывы App Store",
    page_icon="⭐",
    layout="centered",
)

WORK_DIR = Path("/tmp/appstore_reviews_streamlit")
WORK_DIR.mkdir(parents=True, exist_ok=True)


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


def save_available_files(csv_path: Path, checkpoint_path: Path) -> None:
    """Keep generated files available across ordinary Streamlit reruns."""

    if csv_path.exists():
        st.session_state.csv_data = csv_path.read_bytes()
    if checkpoint_path.exists():
        st.session_state.checkpoint_data = checkpoint_path.read_bytes()


if "session_id" not in st.session_state:
    st.session_state.session_id = uuid.uuid4().hex[:12]
if "csv_data" not in st.session_state:
    st.session_state.csv_data = None
if "checkpoint_data" not in st.session_state:
    st.session_state.checkpoint_data = None
if "summary" not in st.session_state:
    st.session_state.summary = None
if "last_log" not in st.session_state:
    st.session_state.last_log = ""


st.title("Сбор отзывов App Store")
st.write(
    "Введите полную ссылку App Store или числовой ID приложения. "
    "На выходе будет CSV только с колонками `Дата`, `Оценка`, `Текст`: "
    "дата в формате ДД.ММ.ГГГГ, оценка — от ★ до ★★★★★."
)

app_value = st.text_input(
    "Ссылка или ID приложения",
    placeholder="https://apps.apple.com/us/app/example/id570060128",
)

all_regions = st.checkbox("Проверить все доступные регионы", value=False)
countries = st.text_input(
    "Коды стран через запятую",
    value="us,gb,de,fr,ru",
    disabled=all_regions,
    help="Для первого теста лучше оставить несколько стран.",
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

if st.button("Начать сбор", type="primary", use_container_width=True):
    try:
        app_id = extract_app_id(app_value)
    except AppStoreReviewsError as exc:
        st.error(str(exc))
        st.stop()

    if not all_regions and not countries.strip():
        st.error("Введите хотя бы один код страны или выберите все регионы.")
        st.stop()
    if start_fresh and uploaded_checkpoint is not None:
        st.error("Нельзя одновременно загрузить checkpoint и начать заново.")
        st.stop()

    session_id = st.session_state.session_id
    csv_path = WORK_DIR / f"reviews_{app_id}_{session_id}.csv"
    checkpoint_path = WORK_DIR / f"checkpoint_{app_id}_{session_id}.sqlite3"

    if start_fresh:
        for path in (
            csv_path,
            checkpoint_path,
            Path(f"{checkpoint_path}-wal"),
            Path(f"{checkpoint_path}-shm"),
        ):
            path.unlink(missing_ok=True)

    if uploaded_checkpoint is not None:
        checkpoint_path.write_bytes(uploaded_checkpoint.getvalue())

    st.session_state.csv_data = None
    st.session_state.checkpoint_data = None
    st.session_state.summary = None
    st.session_state.last_log = ""

    log_placeholder = st.empty()
    handler = StreamlitLogHandler(log_placeholder)
    collector_logger = logging.getLogger("appstore_reviews")
    previous_level = collector_logger.level
    collector_logger.setLevel(logging.INFO)
    collector_logger.addHandler(handler)

    try:
        with st.status("Сбор отзывов выполняется…", expanded=True) as status:
            stats = run(
                app_value,
                output=str(csv_path),
                checkpoint=str(checkpoint_path),
                countries=None if all_regions else countries,
                max_pages=int(max_pages),
                delay=float(delay),
                timeout=float(timeout),
                retries=int(retries),
                reset=False,
            )
            st.session_state.summary = asdict(stats)
            status.update(
                label="Сбор завершён",
                state="complete",
                expanded=False,
            )
            st.success("CSV и checkpoint подготовлены.")
    except (AppStoreReviewsError, sqlite3.DatabaseError, OSError) as exc:
        st.error(str(exc))
    except Exception as exc:
        st.exception(exc)
    finally:
        collector_logger.removeHandler(handler)
        collector_logger.setLevel(previous_level)
        st.session_state.last_log = "\n".join(handler.lines)
        save_available_files(csv_path, checkpoint_path)


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
