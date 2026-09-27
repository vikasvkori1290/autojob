FROM python:3.11-slim

# Install system dependencies and Node.js (for LinkedIn search skill)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    nodejs \
    npm \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Set environment defaults for container
ENV HOST=0.0.0.0
ENV PORT=4000
ENV HEADLESS=1

EXPOSE 4000

# Start GUI server
CMD ["python", "-m", "job_scraper.gui", "--port", "4000", "--no-browser"]
