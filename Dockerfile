FROM python:3.13-slim
WORKDIR /app
COPY poc_network_challenge.py .
ENTRYPOINT ["python3", "poc_network_challenge.py"]
