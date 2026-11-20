FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.116.1 uvicorn==0.35.0
COPY target.py .
COPY target_supervisor.py .
USER 10001
CMD ["python", "target_supervisor.py"]
