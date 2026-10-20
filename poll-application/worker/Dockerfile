FROM maven:3.9-eclipse-temurin-25 AS build

WORKDIR /app

COPY pom.xml .
COPY src ./src
RUN mvn -q -B package

FROM eclipse-temurin:25-jre

WORKDIR /app

COPY --from=build /app/target/worker-jar-with-dependencies.jar ./worker.jar

EXPOSE 80

CMD ["java", "-jar", "worker.jar"]
