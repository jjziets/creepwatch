FROM eclipse-temurin:21-jre-jammy
RUN apt-get update && apt-get install -y curl unzip && rm -rf /var/lib/apt/lists/*
WORKDIR /geyser
COPY geyser-start.sh /geyser-start.sh
RUN chmod +x /geyser-start.sh
ENTRYPOINT ["/geyser-start.sh"]
