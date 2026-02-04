#!/bin/bash -x
#
# DIY rebuild of all binary framework zip files from Apple source.
# This script takes about half an hour to run through.
#
# Make a fork of https://github.com/johnno1962/InstantSyntax
# clone it and run this script inside the clone.
#

export REPO=${1:-https://github.com/apple/swift-syntax}
export REPO_NAME=`basename "$REPO"`
export TAG=${2:-601.0.1}
export DEST="$PWD/$TAG"
export SOURCE="/tmp/$REPO_NAME"
export XCODED=`xcode-select -p`

if [ -f Package.swift ]; then
    export MANIFEST="$PWD/Package.swift.generated"
else
    export MANIFEST="$PWD/Package.swift"
fi

if [ ! -d "$SOURCE" ]; then
  git clone "$REPO" "$SOURCE"
fi

mkdir -p $DEST &&
cd $SOURCE &&
mkdir -p archives &&
git stash &&
git checkout $TAG &&

# Patch Package.swift to expose additional internal targets as library products
sed -i '' '/.library(name: "_SwiftLibraryPluginProvider", targets:/a\
    .library(name: "_SwiftLibraryPluginProviderCShims", targets: ["_SwiftLibraryPluginProviderCShims"]),\
    .library(name: "_SwiftSyntaxCShims", targets: ["_SwiftSyntaxCShims"]),\
    .library(name: "_SwiftSyntaxGenericTestSupport", targets: ["_SwiftSyntaxGenericTestSupport"]),\
    .library(name: "SwiftCompilerPluginMessageHandling", targets: ["SwiftCompilerPluginMessageHandling"]),\
    .library(name: "SwiftLibraryPluginProvider", targets: ["SwiftLibraryPluginProvider"]),\
    .library(name: "SwiftSyntax509", targets: ["SwiftSyntax509"]),\
    .library(name: "SwiftSyntax510", targets: ["SwiftSyntax510"]),\
    .library(name: "SwiftSyntax600", targets: ["SwiftSyntax600"]),\
    .library(name: "SwiftSyntax601", targets: ["SwiftSyntax601"]),
' "$SOURCE/Package.swift"

# Extract module names from patched Package.swift products, excluding _SwiftSyntaxDynamic
export MODULES=$(grep '\.library(name:' "$SOURCE/Package.swift" | \
    sed -E 's/.*\.library\(name: "([^"]+)".*/\1/' | \
    grep -v '_SwiftSyntaxDynamic' | \
    tr '\n' ' ')
export PLATFORMS="${3:-macOS}"
# export PLATFORMS="${3:-macOS iOS iOS_Simulator tvOS_Simulator}"
export CONDITIONS="RESILIENT_LIBRARIES"
export PARALLEL_BUILDS=4
export CONFIG=Release

cat <<PACKAGE >$MANIFEST
// swift-tools-version: 5.9

import CompilerPluginSupport
import PackageDescription

let tag = "$TAG" // $REPO version
let modules: [(name: String, checksum: String)] = [
PACKAGE

# Generate all module entries upfront from $MODULES
for MODULE in $MODULES; do
    echo "    (\"$MODULE\", \"__CHECKSUM__${MODULE}__\")," >> $MANIFEST
done

cat <<'INNER' >/tmp/INNER.sh
PLATFORM=$1
DDATA="$SOURCE/build.$PLATFORM"
DEST_PLATFORM=$(echo $PLATFORM | sed -e 's/_/ /g')
if [ "$PLATFORM" = "macOS" ]; then
    time $XCODED/usr/bin/xcodebuild -scheme $MODULE -quiet -configuration $CONFIG \
        -destination "generic/platform=$DEST_PLATFORM" \
        -archivePath "$SOURCE/archives/$MODULE-$PLATFORM.xcarchive" \
        -derivedDataPath "$DDATA" \
        SKIP_INSTALL=NO BUILD_LIBRARY_FOR_DISTRIBUTION=YES \
        SWIFT_SERIALIZE_DEBUGGING_OPTIONS=NO \
        SWIFT_ACTIVE_COMPILATION_CONDITIONS="$CONDITIONS" \
        SWIFT_VERSION=5 ARCHS="arm64 arm64e x86_64" || exit 1
else
    time $XCODED/usr/bin/xcodebuild -scheme $MODULE -quiet -configuration $CONFIG \
        -destination "generic/platform=$DEST_PLATFORM" \
        -archivePath "$SOURCE/archives/$MODULE-$PLATFORM.xcarchive" \
        -derivedDataPath "$DDATA" \
        SKIP_INSTALL=NO BUILD_LIBRARY_FOR_DISTRIBUTION=YES \
        SWIFT_SERIALIZE_DEBUGGING_OPTIONS=NO \
        SWIFT_ACTIVE_COMPILATION_CONDITIONS="$CONDITIONS" \
        SWIFT_VERSION=5 || exit 1
fi
INNER

for MODULE in $MODULES; do
    export MODULE
    /bin/bash -x <<'OUTER'
    LIBS=""
    cd $SOURCE &&
    echo $PLATFORMS | sed -e 's/ /\n/g' | xargs -P $PARALLEL_BUILDS -I % bash -x /tmp/INNER.sh % &&

    for PLATFORM in $PLATFORMS; do
        DDATA="$SOURCE/build.$PLATFORM"
        LIB="$DDATA/lib$MODULE.a"
        rm -f $DDATA/lib$MODULE*.a &&
        cd $DDATA/Build/Intermediates.noindex/*.build/$CONFIG*/*`echo $MODULE | sed s/^_//`.build/Objects-normal &&
        for ARCH in *; do
            ar qv $DDATA/lib$MODULE.$ARCH.a $ARCH/*.o &&
            ranlib $DDATA/lib$MODULE.$ARCH.a
        done && cd -
        lipo -create $DDATA/lib$MODULE.*.a -output $LIB &&
        LIBS="$LIBS -library $LIB"
    done
        
    rm -rf $DEST/$MODULE.xcframework &&
    $XCODED/usr/bin/xcodebuild -create-xcframework $LIBS -output $DEST/$MODULE.xcframework || exit 1

    cd $DEST/$MODULE.xcframework && for VARIANT in *; do if [ $VARIANT != "Info.plist" ]; then
        # Determine build products directory based on VARIANT prefix
        if [[ $VARIANT == macos-* ]]; then
            PRODUCTS_DIR="$SOURCE/build.macOS/Build/Products/Release"
        elif [[ $VARIANT == ios-arm64 ]]; then
            PRODUCTS_DIR="$SOURCE/build.iOS/Build/Products/Release-iphoneos"
        elif [[ $VARIANT == ios-*-simulator ]]; then
            PRODUCTS_DIR="$SOURCE/build.iOS_Simulator/Build/Products/Release-iphonesimulator"
        elif [[ $VARIANT == tvos-*-simulator ]]; then
            PRODUCTS_DIR="$SOURCE/build.tvOS_Simulator/Build/Products/Release-appletvsimulator"
        fi
        cp -r "$PRODUCTS_DIR/$MODULE.swiftmodule" "$VARIANT/" 2>/dev/null || \
        cp -r "$PRODUCTS_DIR/SwiftSyntax509.swiftmodule" "$VARIANT/$MODULE.swiftmodule" 2>/dev/null || true
    fi done

    cd $DEST && rm -f $MODULE.xcframework.zip $MODULE.xcframework/*/*/*.swiftmodule &&
    codesign -f --timestamp -s "Apple Development: JieHui Lai (4ZZALU97YZ)" $MODULE.xcframework &&
    (zip -r9 --symlinks "$MODULE.xcframework.zip" "$MODULE.xcframework" >>../../zips.txt; \
     CHECKSUM=`swift package compute-checksum "$MODULE.xcframework.zip"`; \
     for MANIFEST in $MANIFEST ../Package.swift; do \
     sed -e "s/[(]\"$MODULE\", \"[^\"]*/(\"$MODULE\", \"$CHECKSUM/g" <$MANIFEST >$MANIFEST.$$ && \
     mv -f $MANIFEST.$$ $MANIFEST; done) &
OUTER
done && sleep 10 && cat <<PACKAGE >>$MANIFEST && echo "Build complete."
]

let package = Package(
  name: "$REPO_NAME",
  platforms: [
    .iOS(.v13),
    .macOS(.v10_15),
    .tvOS(.v13),
    .watchOS(.v6),
  ],
  
  products: modules.map {
      .library(name: \$0.name, targets: [\$0.name])
  },

  targets: modules.map {
      .binaryTarget(
          name: \$0.name,
          path: tag + "/" + "\(\$0.name).xcframework.zip"
    ) }
)
PACKAGE
