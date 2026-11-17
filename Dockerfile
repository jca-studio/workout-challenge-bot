FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY data/missions.csv ./data/missions.csv
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "bot.main"]
