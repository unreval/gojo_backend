FROM python:3.11-slim
WORKDIR /app
COPY Requirements.txt .
RUN pip install -r Requirements.txt
COPY gojo_server.py .
CMD ["python", "gojo_server.py"]