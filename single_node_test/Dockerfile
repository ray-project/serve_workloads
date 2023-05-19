FROM rayproject/ray:latest-py310-gpu

RUN pip install --no-cache-dir transformers==4.26.1
RUN pip install --no-cache-dir timm==0.6.12
RUN pip install --no-cache-dir torch==1.13.1+cu116 --extra-index-url https://download.pytorch.org/whl/cu116
RUN pip install --no-cache-dir torchvision==0.14.1+cu116 --extra-index-url https://download.pytorch.org/whl/cu116
RUN pip install --no-cache-dir scipy==1.10.1
RUN pip install --no-cache-dir numpy==1.24.3
