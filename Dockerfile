FROM python:3.13-slim AS build

RUN pip install --upgrade pip && pip install pipenv

WORKDIR /app

ENV PIPENV_VENV_IN_PROJECT=1

COPY Pipfile* /app/
RUN mkdir /app/.venv
RUN pipenv install --deploy


FROM python:3.13-slim

RUN pip install --upgrade pip && pip install pipenv

WORKDIR /app

COPY . /app/
COPY --from=build /app/.venv /app/.venv

ENV PATH=/app/.venv/bin:$PATH

CMD ["pipenv", "run", "download"]
