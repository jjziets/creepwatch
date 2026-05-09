#!/bin/bash
# Polls GeyserMC's latest build until it supports the target Minecraft version,
# then launches it. Use this when running the latest Minecraft server and you
# need to wait out the lag between MC releases and Geyser support.
TARGET_VERSION="${TARGET_VERSION:-26.1.2}"
GEYSER_URL="https://download.geysermc.org/v2/projects/geyser/versions/latest/builds/latest/downloads/standalone"
JAR="/geyser/geyser.jar"

while true; do
    echo "[geyser-watch] Checking GeyserMC for Java $TARGET_VERSION support..."

    curl -L -s -o /tmp/geyser_check.jar "$GEYSER_URL"

    if unzip -p /tmp/geyser_check.jar org/geysermc/mcprotocollib/protocol/codec/MinecraftCodec.class 2>/dev/null \
        | grep -aq "$TARGET_VERSION"; then

        echo "[geyser-watch] ✅ GeyserMC supports $TARGET_VERSION — starting!"
        cp /tmp/geyser_check.jar "$JAR"
        rm -f /tmp/geyser_check.jar
        exec java -jar "$JAR" --nogui
    else
        echo "[geyser-watch] ⏳ Not yet supported. Retrying in 1 hour..."
        rm -f /tmp/geyser_check.jar
        sleep 3600
    fi
done
