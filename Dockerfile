# Base image with Python 3
FROM python:3.12-slim

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install Rust via rustup
ENV RUSTUP_HOME=/root/.rustup \
    CARGO_HOME=/root/.cargo
ENV PATH="${CARGO_HOME}/bin:${PATH}"

RUN curl https://sh.rustup.rs -sSf \
    | sh -s -- -y --default-toolchain stable

# Install maturin (needs cargo in PATH)
RUN pip install --no-cache-dir maturin poetry flask sqlalchemy

# Your project
WORKDIR /app
COPY . .

# Example build command (optional)
# RUN maturin build --release -b wheel

CMD ["bash"]