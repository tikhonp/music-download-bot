ARG PYTHON_VERSION=3.14.8

FROM python:${PYTHON_VERSION}-alpine

LABEL dev.dozzle.icon="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+PHJlY3Qgd2lkdGg9IjI0IiBoZWlnaHQ9IjI0IiByeD0iNSIgZmlsbD0iIzIyOUVEOSIvPjxwYXRoIGZpbGw9IiNmZmYiIGQ9Ik0xNiA1djkuNWEyLjc1IDIuNzUgMCAxIDEtMS41LTIuNDVWOC4ybC00IC45djcuNGEyLjc1IDIuNzUgMCAxIDEtMS41LTIuNDVWNy4zeiIvPjwvc3ZnPg=="

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

ARG UID=10001
RUN adduser \
    --disabled-password \
    --gecos "" \
    --home "/nonexistent" \
    --shell "/sbin/nologin" \
    --no-create-home \
    --uid "${UID}" \
    appuser

RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=bind,source=requirements.txt,target=requirements.txt \
    python -m pip install -r requirements.txt

USER appuser

COPY main.py ./

CMD ["python", "main.py"]
