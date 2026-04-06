FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml .
RUN pip install --no-cache-dir .

COPY veyra/ veyra/
COPY alembic/ alembic/
COPY alembic.ini .

EXPOSE 5678
CMD ["python", "-m", "veyra.main"]
