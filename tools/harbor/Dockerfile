# Video-Arena sandbox for Harbor (Protocol B): offline, with the tools pre-installed.
# The same image is the verifier: scoring needs only python3 and the case's judge (stdlib).
#
#   docker build -t video-arena/base:latest tools/harbor
#
# Everything the agent may use is installed here, because the sandbox has no network at run
# time: ffmpeg/ffprobe to read the replay, Python with the usual video and numeric stack, and
# tesseract for on-screen text. Deliberately absent: game engines, engine weights, ground truth,
# and model weights of any kind.
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLBACKEND=Agg

# tmux (and asciinema) are what Harbor's terminal agents such as terminus-2 drive the shell
# through; installed here because they cannot be fetched once the trial is offline.
# libgl1/libglib2.0-0 are the shared objects opencv links against.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg tesseract-ocr tesseract-ocr-eng \
        tmux asciinema \
        jq bc file procps less vim-tiny \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install \
        numpy scipy pandas \
        pillow opencv-python-headless scikit-image imageio imageio-ffmpeg av \
        matplotlib pytesseract tqdm

# /app is the case folder. The task mounts match.mp4 and instruction.md into it read-only;
# answer.json is the pre-seeded empty answer (the schema's root key, an empty list), which the
# agent overwrites in place. Every game uses the same root key, so one seed serves all 180.
WORKDIR /app
RUN printf '{"events": []}\n' > /app/answer.json \
    && chmod 666 /app/answer.json \
    && mkdir -p /logs/verifier /logs/agent /logs/artifacts

# The unprivileged user the agent runs as ([agent].user in every task.toml): agent harnesses
# such as Claude Code misbehave as root. It owns /app (scratch space next to the read-only
# inputs, as in the native sandbox); the verifier stays root.
RUN useradd -m -u 1000 -s /bin/bash agent && chown agent /app

CMD ["sh", "-c", "sleep infinity"]
