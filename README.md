# App Store Reviews Streamlit

Streamlit interface for collecting the maximum set of App Store reviews exposed
by Apple's public customer-review RSS feeds.

## Files

- `appstore_reviews.py` — collector extracted from the Colab notebook, without
  the Colab-only automatic execution block.
- `streamlit_app.py` — web interface.
- `requirements.txt` — Python dependencies for Streamlit Community Cloud.

## Local run

```bash
python -m pip install -r requirements.txt
streamlit run streamlit_app.py
```

The generated semicolon-separated CSV has exactly three columns: `Дата`,
`Оценка`, and `Текст`. Dates use `ДД.ММ.ГГГГ`; ratings use `★`–`★★★★★`.

The Streamlit parameters are submitted as one form. Collection runs
synchronously with a live progress bar, while CSV/checkpoint data and the final
summary remain available across ordinary reruns through Session State. The
"all regions" option uses the 175 storefronts listed by Apple rather than every
ISO country code. Working files are removed after each run; abandoned files are
cleaned after 24 hours. A saved checkpoint is reused only for the same app ID.
The project config explicitly limits uploaded checkpoint files to 200 MB.
