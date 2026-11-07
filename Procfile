web: python manage.py migrate --noinput && python manage.py seed_radlea && python manage.py bootstrap_director && gunicorn config.wsgi --bind 0.0.0.0:8080 --workers 2 --worker-tmp-dir /dev/shm
