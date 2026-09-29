"""gunicorn settings for the web process (the worker runs separately)."""

import multiprocessing
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
# Threads, not only processes: most time is spent waiting on the database
# and, for CV analysis, on the user's AI provider.
workers = int(os.environ.get("WEB_CONCURRENCY", min(4, multiprocessing.cpu_count() * 2)))
threads = int(os.environ.get("WEB_THREADS", 8))
worker_class = "gthread"
# CV analysis waits on an AI provider for up to ~2 minutes.
timeout = 180
graceful_timeout = 30
keepalive = 5
# Recycle processes now and then to bound any slow memory growth.
max_requests = 2000
max_requests_jitter = 200
limit_request_line = 8190
forwarded_allow_ips = "*"          # behind the reverse proxy in docker-compose
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info")
