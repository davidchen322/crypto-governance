# The stock apache/iceberg-rest-fixture image bundles only the SQLite JDBC driver — its
# default catalog URI is `jdbc:sqlite::memory:`. Backing the catalog with Postgres (so
# table metadata survives `docker compose down`) therefore needs the Postgres driver added.
#
# The image's CMD is `java -jar iceberg-rest-adapter.jar`, and `-jar` makes the JVM ignore
# both -cp and $CLASSPATH. So the driver cannot simply be dropped in — CMD is replaced with
# an explicit classpath invocation of the same main class.

ARG PG_JDBC_VERSION=42.7.4

FROM curlimages/curl:8.10.1 AS driver
ARG PG_JDBC_VERSION
RUN curl -fsSL -o /tmp/postgresql.jar \
    "https://repo1.maven.org/maven2/org/postgresql/postgresql/${PG_JDBC_VERSION}/postgresql-${PG_JDBC_VERSION}.jar"

FROM apache/iceberg-rest-fixture:1.9.2
COPY --from=driver /tmp/postgresql.jar /usr/lib/iceberg-rest/postgresql.jar
CMD ["java", "-cp", \
     "/usr/lib/iceberg-rest/iceberg-rest-adapter.jar:/usr/lib/iceberg-rest/postgresql.jar", \
     "org.apache.iceberg.rest.RESTCatalogServer"]
