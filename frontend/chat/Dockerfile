FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install backend requirements first
COPY backend/requirements.txt /tmp/backend-requirements.txt
RUN pip install --no-cache-dir -r /tmp/backend-requirements.txt

# Copy and install chat requirements
COPY frontend/chat/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY backend/app /app/app

# Copy chat app
COPY frontend/chat/ /app/

# Set PYTHONPATH
ENV PYTHONPATH="/app"

EXPOSE 8080

CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8080", "--headless"]
