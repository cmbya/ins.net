FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 INS_DATA=/data INS_PORT=18080
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir gallery-dl==1.32.13 yt-dlp==2026.08.19
WORKDIR /app
COPY insnet/ /app/insnet/
COPY static/ /app/static/
RUN mkdir /data && chown -R 10001:10001 /data /app
USER 10001:10001
EXPOSE 18080
CMD ["python", "-m", "insnet.web"]
