FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5555

CMD ["gunicorn", "-b", "0.0.0.0:5555", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
