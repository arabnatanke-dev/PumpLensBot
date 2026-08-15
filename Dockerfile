# Build and runtime comments are bilingual for maintainers.
# Комментарии сборки и запуска продублированы для разработчиков.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md requirements.lock ./
COPY src ./src
RUN pip install --upgrade pip \
    && pip install --requirement requirements.lock \
    && pip install --no-deps .

COPY settings.yaml ./settings.yaml
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 pumplens \
    && mkdir -p /app/data \
    && chown -R pumplens:pumplens /app/data
USER pumplens

ENTRYPOINT ["pumplens"]
CMD ["serve"]
