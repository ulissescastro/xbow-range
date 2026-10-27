FROM python:3.12-alpine

WORKDIR /app

COPY app.py /app/app.py

RUN adduser -D -u 10001 app
USER app

ENV PORT=8800 \
    XBOW_STATE_DIR=/state \
    PYTHONUNBUFFERED=1

EXPOSE 8800

# The healthcheck uses the same single endpoint that clients use.
HEALTHCHECK --interval=10s --timeout=3s --start-period=3s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/validate' % os.environ.get('PORT','8800'), timeout=2)"

CMD ["python", "/app/app.py"]
