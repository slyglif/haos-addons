ARG BUILD_FROM
FROM $BUILD_FROM

# Copy data for add-on
RUN apk add --no-cache python3 py3-pip py3-pyaml tini
RUN pip3 install --break-system-packages pypowerwall paho-mqtt

COPY rootfs /
COPY app /app
RUN chmod a+rx /docker-entrypoint.sh

ENTRYPOINT [ "/sbin/tini", "--", "/docker-entrypoint.sh" ]
