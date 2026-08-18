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

The generated CSV has exactly three columns: `date`, `rating`, and `text`.
