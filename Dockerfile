FROM mysterysd/wzmlx:v3

WORKDIR /usr/src/app
RUN chmod 777 /usr/src/app

# Use system Python in the base image to keep compatibility with prebuilt deps.
RUN python3 -m venv .venv --system-site-packages

COPY requirements.txt .
RUN .venv/bin/pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["bash", "start.sh"]

