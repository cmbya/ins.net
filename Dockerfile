FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 INS_DATA=/data INS_PORT=18080
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update -o Acquire::Retries=3 -o APT::Update::Error-Mode=any \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir gallery-dl==1.32.13 yt-dlp==2026.08.19 instaloader==4.15.3
WORKDIR /app
COPY insnet/ /app/insnet/
COPY static/ /app/static/
RUN mkdir -p /data /archive && chown -R 10001:10001 /data /archive /app
EXPOSE 18080
CMD ["python", "-m", "insnet.entrypoint"]
