web: gunicorn stock_manager.wsgi --bind 0.0.0.0:$PORT --workers 3 --timeout 90 --graceful-timeout 30 --max-requests 200 --max-requests-jitter 50
