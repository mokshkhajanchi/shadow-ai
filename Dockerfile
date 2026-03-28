FROM python:3.12-slim

WORKDIR /app

# Install Claude Code CLI (required by claude-agent-sdk)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir -e .

# Copy config files
COPY .env.example .env.example

# Run the bot
CMD ["shadow-ai"]
