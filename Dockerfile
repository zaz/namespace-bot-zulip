FROM python:3.12-slim

# Tools you'll likely want available to the shell the bot exposes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Unbuffered stdout/stderr so logs stream out in real time.
ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shell_bot.py .

# Credentials and the allowlist are provided at runtime via -e env vars:
#   ZULIP_EMAIL, ZULIP_API_KEY, ZULIP_SITE, SHELL_BOT_ALLOWED_SENDERS
CMD ["python", "shell_bot.py"]
