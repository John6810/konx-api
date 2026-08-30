FROM python:3.12-slim
WORKDIR /app
# tzdata : nécessaire pour ZoneInfo("Europe/Brussels") sur l'image slim.
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx tzdata
COPY main.py .
USER 1000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
