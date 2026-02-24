FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py /app/main.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
