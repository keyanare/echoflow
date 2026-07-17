FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY main.py ytdl_source.py ./

CMD ["uv", "run", "--frozen", "--no-sync", "main.py"]
