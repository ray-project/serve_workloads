# Dockerfile for calculator apps
FROM rayproject/ray-ml:2.6.3-py39-gpu

# Install curl
RUN sudo apt-get update && sudo apt-get install -y curl

# Install required dependencies
RUN pip install python-multipart==0.0.6

# Install debugging tools
RUN pip install memray==1.9.1
RUN pip install locust==2.16.1

# Download HuggingFace models
COPY model_downloader.py .
RUN python model_downloader.py

# Install grpcurl
RUN wget https://github.com/fullstorydev/grpcurl/releases/download/v1.8.7/grpcurl_1.8.7_linux_x86_64.tar.gz
RUN tar -xvf grpcurl_1.8.7_linux_x86_64.tar.gz
RUN sudo chmod +x grpcurl

# Copy Serve files to working directory
COPY small_calculator.py .
COPY base_calculator.py .

# Copy node preemption helper script
COPY preempt_node.py .

# Copy helfpul debugging files to working directory
COPY calculator_client.py .
COPY constant_load.py .
