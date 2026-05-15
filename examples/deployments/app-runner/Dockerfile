FROM python:3.12-slim

# Run as non-root for App Runner best-practice
RUN useradd -m -u 10001 app
USER app
WORKDIR /home/app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/home/app/.local/bin:${PATH}"

COPY --chown=app:app requirements.txt /home/app/
RUN pip install --user -r requirements.txt

COPY --chown=app:app agent_service.py /home/app/

# App Runner sends HTTP traffic to the container on the configured
# port (default 8080). Match it here.
EXPOSE 8080

CMD ["uvicorn", "agent_service:app", "--host", "0.0.0.0", "--port", "8080"]
