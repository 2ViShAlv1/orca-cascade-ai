# ORCA - zero third-party dependencies, so the slim base image is all it needs.
FROM python:3.10-slim
WORKDIR /app
COPY orca ./orca
COPY web ./web
COPY models ./models
COPY reports ./reports
COPY tests ./tests
COPY run.py README.md ./
EXPOSE 8000
CMD ["python", "run.py", "--host", "0.0.0.0", "--port", "8000"]
