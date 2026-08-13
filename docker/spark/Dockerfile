# Iceberg jars are baked in at build time rather than pulled with `--packages` at run
# time: `--packages` re-resolves on every invocation, needs network, and turns a flaky
# Maven mirror into a flaky test suite.

ARG SPARK_VERSION=3.5.3
ARG ICEBERG_VERSION=1.6.1

FROM curlimages/curl:8.10.1 AS jars
ARG ICEBERG_VERSION
WORKDIR /jars
RUN curl -fsSL -O "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/${ICEBERG_VERSION}/iceberg-spark-runtime-3.5_2.12-${ICEBERG_VERSION}.jar" \
 && curl -fsSL -O "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/${ICEBERG_VERSION}/iceberg-aws-bundle-${ICEBERG_VERSION}.jar"

FROM apache/spark:3.5.3
USER root
COPY --from=jars /jars/*.jar /opt/spark/jars/
COPY spark-defaults.conf /opt/spark/conf/spark-defaults.conf
RUN mkdir -p /opt/app && chown -R spark:spark /opt/app
USER spark
WORKDIR /opt/app
