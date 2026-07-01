# Rabble on Render (Docker deploy).
#
# Playwright's Python image ships with Chromium + all its OS deps already
# installed, so we don't need `playwright install --with-deps` in the build.
# The frontend is built and committed to frontend/dist, so no Node needed.

FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy

WORKDIR /app

# Install backend deps first so the layer cache survives frontend edits.
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# App code + pre-built frontend bundle
COPY backend /app/backend
COPY frontend/dist /app/frontend/dist

# Playwright browsers are already installed in the base image at
# /ms-playwright — point the runtime at them.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

# Render sets $PORT. Default to 8000 for local `docker run`.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir /app/backend"]
