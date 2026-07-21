FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN pip install --no-cache-dir .

# The parent wordsman checkout is mounted at /wordsman (see docker-compose.yml).
ENV TG_BOT_WORDSMAN_ROOT=/wordsman
EXPOSE 8340
CMD ["tg-bot", "serve"]
