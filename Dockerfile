# One image, three roles: build with --build-arg COMPONENT=api|worker.
#
# The packages are installed editable from the same tree, exactly as they are
# in development, so a container never runs different code from the tests.
FROM python:3.13-slim

ARG COMPONENT=api

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy the packaging metadata first so a code change does not re-resolve deps.
COPY pyproject.toml ./
COPY core/pyproject.toml core/
COPY api/pyproject.toml api/
COPY worker/pyproject.toml worker/
RUN mkdir -p core/src/aisales api/src/aisales_api worker/src/aisales_worker \
    && touch core/src/aisales/__init__.py api/src/aisales_api/__init__.py \
             worker/src/aisales_worker/__init__.py

COPY core/ core/
COPY api/ api/
COPY worker/ worker/
COPY scripts/ scripts/

# api pulls in fastapi and uvicorn; worker needs neither, so installing only
# what a role uses keeps the worker image small and its attack surface smaller.
RUN if [ "$COMPONENT" = "api" ]; then \
        pip install --no-cache-dir -e core/ -e api/ -e worker/; \
    else \
        pip install --no-cache-dir -e core/ -e worker/; \
    fi

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
CMD ["python", "-m", "aisales_api", "--host", "0.0.0.0", "--port", "8000"]
