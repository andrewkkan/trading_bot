FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install ipython

# Copy source
COPY . .

CMD ["tail", "-f", "/dev/null"]
