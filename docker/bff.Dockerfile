FROM eclipse-temurin:25-jre-alpine@sha256:b7528962e65d43064d8a8b67ba175367896bc948363ea2d3fcd7f10d34e1f048
RUN addgroup -S portal && adduser -S -G portal -H -h /app portal
WORKDIR /app
COPY --chown=root:root app.jar /app/app.jar
COPY --chown=root:root bff-entrypoint.sh /app/entrypoint.sh
USER portal
EXPOSE 8080
ENTRYPOINT ["/bin/sh", "/app/entrypoint.sh"]
