# One image, one service: the API and the built frontend served from the same
# origin. Two stages so the Node toolchain used to build the bundle does not
# ship in the final image.

# --- stage 1: build the frontend ------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build
# Fail loudly here rather than shipping an image whose frontend is missing.
# A build that "succeeds" into a 404 is far more expensive to diagnose than
# one that stops at the step that actually went wrong.
RUN test -f /build/dist/index.html \
    && echo "--- frontend build output ---" && ls -la /build/dist

# --- stage 2: the runtime --------------------------------------------------
FROM python:3.12-slim
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY --from=frontend /build/dist ./static
RUN test -f /app/static/index.html \
    && echo "--- static copied into image ---" && ls -la /app/static

ENV PROVIDER=replay \
    POLL_INTERVAL_SECONDS=10 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# Render and most PaaS hosts inject $PORT; fall back to 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
