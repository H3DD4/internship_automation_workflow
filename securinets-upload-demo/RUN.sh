#!/usr/bin/env bash
# Start the vulnerable lab on http://localhost:8000
cd "$(dirname "$0")/app"
echo "=================================================="
echo " Securinets Photo Club — VULNERABLE demo"
echo " http://localhost:8000"
echo " Ctrl-C to stop"
echo "=================================================="
php -S 0.0.0.0:8000
