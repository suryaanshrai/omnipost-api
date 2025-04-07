FROM python:3.12.3-slim

WORKDIR /app

RUN pip install poetry==1.8.2

COPY pyproject.toml poetry.lock ./

RUN poetry config virtualenvs.create false \
    && poetry install --no-root

COPY app .

# Create directory for logs
RUN mkdir -p logs

# Create an entrypoint script to run both server and worker
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
# Wait for Redis to be ready\n\
echo "Waiting for Redis..."\n\
until python -c "import redis; redis.Redis(host=\"redis\").ping()" 2>/dev/null; do\n\
  echo "Redis not available yet - sleeping for 1 second"\n\
  sleep 1\n\
done\n\
echo "Redis is ready!"\n\
\n\
# Function to gracefully stop processes on container shutdown\n\
cleanup() {\n\
  echo "Stopping processes..."\n\
  kill -TERM "$SERVER_PID" 2>/dev/null || true\n\
  kill -TERM "$WORKER_PID" 2>/dev/null || true\n\
  wait "$SERVER_PID" 2>/dev/null || true\n\
  wait "$WORKER_PID" 2>/dev/null || true\n\
  echo "Processes stopped"\n\
  exit 0\n\
}\n\
\n\
# Setup trap for graceful shutdown\n\
trap cleanup SIGTERM SIGINT\n\
\n\
# Start Gunicorn in the background with logs\n\
echo "Starting Gunicorn server..."\n\
gunicorn --reload --bind 0.0.0.0:8000 app.wsgi:application > logs/server_logs 2>&1 &\n\
SERVER_PID=$!\n\
\n\
# Start RQ Worker in the background with logs\n\
echo "Starting RQ Worker..."\n\
python manage.py rqworker --with-scheduler default > logs/worker_logs 2>&1 &\n\
WORKER_PID=$!\n\
\n\
echo "All processes started. Server PID: $SERVER_PID, Worker PID: $WORKER_PID"\n\
\n\
# Wait for either process to exit\n\
wait -n "$SERVER_PID" "$WORKER_PID"\n\
\n\
# If we get here, one of the processes exited unexpectedly\n\
EXIT_CODE=$?\n\
echo "Process exited unexpectedly with code $EXIT_CODE"\n\
exit $EXIT_CODE\n\
' > /app/entrypoint.sh

# Make the entrypoint script executable
RUN chmod +x /app/entrypoint.sh

# Check if init_db.py exists and run it conditionally
RUN if [ -f scripts/init_db.py ]; then python scripts/init_db.py; else echo "No init_db.py found, skipping"; fi

# Set entrypoint to our script
ENTRYPOINT ["/app/entrypoint.sh"]