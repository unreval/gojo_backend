FROM python:3.11-slim

WORKDIR /app

COPY gojo_backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY gojo_backend/ ./

EXPOSE 8080

CMD ["python", "gojo_server.py"]
