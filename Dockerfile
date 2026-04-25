FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*


RUN pip install torch --index-url https://download.pytorch.org/whl/cpu

COPY Requirements.txt .
RUN pip install -r Requirements.txt

COPY gojo_server.py .
COPY gojo_index.json .

CMD ["python", "gojo_server.py"]