ARG BUILD_FROM
FROM ${BUILD_FROM}

# Pillow + numpy are enough for cheap frame-diff motion; no heavyweight CV stack.
RUN apk add --no-cache jpeg zlib

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt

COPY watcher /app/watcher
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app
CMD ["/run.sh"]
