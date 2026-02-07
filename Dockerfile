FROM python:3.11-slim

WORKDIR /app

# Sem dependências externas (stdlib only), então não precisa pip install.
COPY main.py /app/main.py

ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
