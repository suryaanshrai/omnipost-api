FROM python:3.12.3-slim

WORKDIR /app

RUN pip install poetry==1.8.2

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false \
    && poetry install --no-root

COPY app .

RUN python scripts/init_db.py

CMD ["gunicorn", "--reload", "--bind", "0.0.0.0:8000","app.wsgi:application" ]