# Dockerfile for calculator apps
FROM rayproject/ray-ml:2.6.3-py39-gpu

# Install curl
RUN apt-get update && apt-get install -y curl

# Install required dependencies
RUN pip install python-multipart==0.0.6

# Install debugging tools
RUN pip install memray==1.9.1
RUN pip install locust==2.16.1

# Download HuggingFace models
COPY model_downloader.py .
RUN python model_downloader.py

# Copy Serve files to working directory
COPY small_calculator.py .
COPY base_calculator.py .

# Copy helfpul debugging files to working directory
COPY calculator_client.py .
COPY constant_load.py .
