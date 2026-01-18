FROM mysterysd/wzmlx:v3

WORKDIR /usr/src/app
RUN chmod 777 /usr/src/app

# Build and install MEGA SDK Python bindings (MegaApi).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        swig \
        pkg-config \
        python3-dev \
        libssl-dev \
        libcrypto++-dev \
        libsodium-dev \
        libcurl4-openssl-dev \
        libicu-dev \
        libsqlite3-dev \
        libavformat-dev \
        libavcodec-dev \
        libavutil-dev \
        libswscale-dev \
        libswresample-dev \
        libc-ares-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 https://github.com/meganz/sdk.git /tmp/meganz-sdk \
    && mkdir -p /tmp/meganz-sdk/build \
    && cd /tmp/meganz-sdk/build \
    && cmake -DENABLE_PYTHON=ON -DCMAKE_BUILD_TYPE=Release -DUSE_PDFIUM=OFF .. \
    && make -j"$(nproc)" \
    && cd /tmp/meganz-sdk/bindings/python \
    && python3 setup.py build \
    && python3 setup.py install \
    && cd / \
    && rm -rf /tmp/meganz-sdk

# Use system Python in the base image to keep compatibility with prebuilt deps.
RUN python3 -m venv .venv --system-site-packages

COPY requirements.txt .
RUN .venv/bin/pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "start.sh"]

