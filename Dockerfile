FROM python:3.12-slim
WORKDIR /app
COPY server.py .
COPY static ./static
ENV PORT=9000 HOST=0.0.0.0
EXPOSE 9000
CMD ["python", "server.py"]
