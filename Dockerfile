# Build and runtime comments are bilingual for maintainers.
# Комментарии сборки и запуска продублированы для разработчиков.
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install '.[telegram,storage,security,webapp]'

COPY settings.yaml ./settings.yaml
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

RUN useradd --create-home --uid 10001 pumplens
USER pumplens

ENTRYPOINT ["pumplens"]
CMD ["serve"]
