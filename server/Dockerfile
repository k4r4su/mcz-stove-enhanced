FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

RUN useradd --system --uid 10001 mcz && mkdir -p /srv/data && chown mcz /srv/data
USER mcz
VOLUME ["/srv/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4)"
CMD ["uvicorn", "app.asgi:app", "--host", "0.0.0.0", "--port", "8000"]
