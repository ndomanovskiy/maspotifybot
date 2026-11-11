FROM python:3.12-slim

WORKDIR /app

# constraints.txt pins every package to the versions the running bot was tested with
COPY pyproject.toml constraints.txt ./
RUN pip install --no-cache-dir -c constraints.txt .

COPY app/ app/

CMD ["python", "-m", "app.main"]
