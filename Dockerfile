FROM python:3.12.3

WORKDIR /app

RUN pip install poetry==1.8.2

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false \
    && poetry install --no-root

COPY app .

CMD python manage.py rqworker --with-scheduler & python manage.py runserver 0.0.0.0:8000