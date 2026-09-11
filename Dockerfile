FROM mcr.microsoft.com/playwright/python:v1.55.0-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qa9210.py qa10.py qa11.py ./
COPY api ./api
RUN mkdir -p /app/reports && useradd --create-home --uid 10001 qa && chown -R qa:qa /app
USER qa
ENV QA_REPORT_ROOT=/app/reports
EXPOSE 8000
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
