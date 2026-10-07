#!/usr/bin/env bash
# Builds the qwayland Quest client without Gradle, using only the Android SDK.
set -euo pipefail
cd "$(dirname "$0")"
SDK=${ANDROID_HOME:-$HOME/Android/Sdk}
BT=$SDK/build-tools/$(ls "$SDK/build-tools" | sort -V | tail -1)
PLATFORM=$SDK/platforms/android-34/android.jar
OUT=build
rm -rf "$OUT" && mkdir -p "$OUT/classes" "$OUT/res"

"$BT/aapt2" compile --dir res -o "$OUT/res/compiled.zip"
"$BT/aapt2" link -I "$PLATFORM" --manifest AndroidManifest.xml -o "$OUT/unsigned.apk" "$OUT/res/compiled.zip"

javac -source 11 -target 11 -Xlint:-options -encoding UTF-8 \
    -classpath "$PLATFORM" -d "$OUT/classes" $(find src -name '*.java')
"$BT/d8" --min-api 29 --lib "$PLATFORM" --output "$OUT" $(find "$OUT/classes" -name '*.class')

(cd "$OUT" && zip -q unsigned.apk classes.dex)
"$BT/zipalign" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

KEYSTORE=${QWAYLAND_KEYSTORE:-$HOME/.android/debug.keystore}
if [ ! -f "$KEYSTORE" ]; then
    keytool -genkeypair -keystore "$KEYSTORE" -storepass android -keypass android -alias androiddebugkey \
        -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug,O=Android,C=US"
fi
"$BT/apksigner" sign --ks "$KEYSTORE" --ks-pass pass:android --key-pass pass:android \
    --out "$OUT/qwayland.apk" "$OUT/aligned.apk"
echo "built $OUT/qwayland.apk"
