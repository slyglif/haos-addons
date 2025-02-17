ARG BUILD_FROM
FROM $BUILD_FROM

# Copy data for add-on
COPY run.sh /
RUN chmod a+rx /run.sh

CMD [ "/run.sh" ]
