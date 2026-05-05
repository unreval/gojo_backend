FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY gojo_server.py .
CMD ["python", "gojo_server.py"]