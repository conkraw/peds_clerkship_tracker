# Optional: institutional/private hosting. Never publish this image publicly:
# the application embeds confidential student-specific configuration.
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 appuser
COPY --chown=appuser:appuser peds_clerkship_tracker.py ./
COPY --chown=appuser:appuser .streamlit/config.toml ./.streamlit/config.toml
USER appuser
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "peds_clerkship_tracker.py", "--server.port=8501", "--server.address=0.0.0.0"]
