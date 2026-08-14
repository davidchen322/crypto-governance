# Jars are baked in at build time rather than pulled with `--packages` at run time:
# `--packages` re-resolves on every invocation, needs network, and turns a flaky Maven
# mirror into a flaky test suite.

# Pinned to match the catalog image tag in docker-compose.yml. `apache/iceberg-rest-fixture`
# publishes no tag below 1.8.1, so 1.6.x is not an option on the catalog side.
ARG SPARK_VERSION=3.5.3
ARG ICEBERG_VERSION=1.9.2
# hadoop-aws MUST match the Hadoop version Spark bundles (3.3.4 for Spark 3.5.3) — a
# mismatch fails at class-load with NoSuchMethodError, not at build. aws-java-sdk-bundle
# is the SDK v1 pairing hadoop-aws 3.3.x expects; it coexists with iceberg-aws-bundle's
# SDK v2 because they occupy different packages (com.amazonaws vs software.amazon.awssdk).
ARG HADOOP_VERSION=3.3.4
ARG AWS_SDK_VERSION=1.12.262

FROM curlimages/curl:8.10.1 AS jars
ARG ICEBERG_VERSION
ARG HADOOP_VERSION
ARG AWS_SDK_VERSION
WORKDIR /jars
RUN curl -fsSL -O "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/${ICEBERG_VERSION}/iceberg-spark-runtime-3.5_2.12-${ICEBERG_VERSION}.jar" \
 && curl -fsSL -O "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/${ICEBERG_VERSION}/iceberg-aws-bundle-${ICEBERG_VERSION}.jar" \
 && curl -fsSL -O "https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/${HADOOP_VERSION}/hadoop-aws-${HADOOP_VERSION}.jar" \
 && curl -fsSL -O "https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/${AWS_SDK_VERSION}/aws-java-sdk-bundle-${AWS_SDK_VERSION}.jar"

FROM apache/spark:3.5.3
USER root
COPY --from=jars /jars/*.jar /opt/spark/jars/
COPY spark-defaults.conf /opt/spark/conf/spark-defaults.conf
RUN mkdir -p /opt/app && chown -R spark:spark /opt/app
# The base image leaves /opt/spark/bin off PATH, so `docker compose exec spark spark-sql`
# fails with "executable file not found" until this is set.
ENV PATH="/opt/spark/bin:/opt/spark/sbin:${PATH}"
ENV PYTHONPATH="/opt/app:${PYTHONPATH}"
USER spark
WORKDIR /opt/app
