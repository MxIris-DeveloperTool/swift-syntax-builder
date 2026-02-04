#!/bin/bash -x
#
# DIY rebuild of all binary framework zip files from Apple source.
# Generates Package.swift with _Aggregation target pattern.
#
# Usage:
#   ./package_syntax_v2.sh [OPTIONS]
#
# Options:
#   --repo URL          Repository URL (default: https://github.com/apple/swift-syntax)
#   --tag VERSION       Tag/version to build (default: 601.0.1)
#   --platforms LIST    Platforms to build, space-separated (default: "macOS")
#                       Examples: "macOS" "macOS iOS" "macOS iOS iOS_Simulator"
#   --mode MODE         "local" for local paths, "remote" for URLs (default: local)
#   --base-url URL      Base URL for remote xcframeworks (required if mode=remote)
#   --output FILE       Output Package.swift path (default: ./Package.swift)
#   --help              Show this help message
#
# Examples:
#   ./package_syntax_v2.sh --tag 601.0.1 --platforms "macOS"
#   ./package_syntax_v2.sh --tag 601.0.1 --mode remote --base-url "https://example.com/frameworks"

set -e

# Default values
REPO="https://github.com/apple/swift-syntax"
TAG="601.0.1"
PLATFORMS="macOS"
MODE="local"
BASE_URL=""
OUTPUT_FILE="$PWD/Package.swift"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --repo)
            REPO="$2"
            shift 2
            ;;
        --tag)
            TAG="$2"
            shift 2
            ;;
        --platforms)
            PLATFORMS="$2"
            shift 2
            ;;
        --mode)
            MODE="$2"
            shift 2
            ;;
        --base-url)
            BASE_URL="$2"
            shift 2
            ;;
        --output)
            OUTPUT_FILE="$2"
            shift 2
            ;;
        --help)
            head -30 "$0" | tail -27
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Validate arguments
if [[ "$MODE" == "remote" && -z "$BASE_URL" ]]; then
    echo "Error: --base-url is required when --mode is 'remote'"
    exit 1
fi

export REPO
export REPO_NAME=$(basename "$REPO")
export TAG
export DEST="$PWD/$TAG"
export SOURCE="/tmp/$REPO_NAME"
export XCODED=$(xcode-select -p)
export PLATFORMS
export MODE
export BASE_URL
export OUTPUT_FILE
export CONDITIONS="RESILIENT_LIBRARIES"
export PARALLEL_BUILDS=4
export CONFIG=Release

# Clone repository if needed
if [ ! -d "$SOURCE" ]; then
    git clone "$REPO" "$SOURCE"
fi

mkdir -p "$DEST"
cd "$SOURCE"
mkdir -p archives
git stash || true
git checkout "$TAG"

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

echo "Modules to build: $MODULES"

# ============================================================================
# Parse dependencies from swift-syntax Package.swift
# ============================================================================
parse_dependencies() {
    local PACKAGE_FILE="$SOURCE/Package.swift"

    # Use a Python script to parse the Package.swift and extract dependencies
    python3 - "$PACKAGE_FILE" << 'PYTHON_SCRIPT'
import re
import sys

package_file = sys.argv[1]

with open(package_file, 'r') as f:
    content = f.read()

# Find all .target definitions (not .testTarget)
# Pattern matches .target(name: "XXX", dependencies: [...])
target_pattern = r'\.target\s*\(\s*name:\s*"([^"]+)"[^)]*?dependencies:\s*\[([^\]]*)\]'

targets = {}

for match in re.finditer(target_pattern, content, re.DOTALL):
    target_name = match.group(1)
    deps_str = match.group(2)

    # Skip test targets by checking if name ends with Test or TestSupport related patterns
    # Actually we want all targets, the pattern already excludes .testTarget

    # Parse dependencies - can be strings or .target(name:) or .product(name:)
    deps = []

    # Match string dependencies: "DepName"
    string_deps = re.findall(r'"([^"]+)"', deps_str)
    deps.extend(string_deps)

    # Filter out test-only dependencies and external packages
    deps = [d for d in deps if not d.startswith('_SwiftSyntaxTestSupport')
            and not d.startswith('_InstructionCounter')
            and not d.endswith('Test')]

    targets[target_name] = deps

# Also find targets without dependencies
no_dep_pattern = r'\.target\s*\(\s*name:\s*"([^"]+)"[^)]*?\)'
for match in re.finditer(no_dep_pattern, content, re.DOTALL):
    target_name = match.group(1)
    if target_name not in targets:
        # Check if this target block doesn't have dependencies
        block_start = match.start()
        block_end = match.end()
        block = content[block_start:block_end]
        if 'dependencies:' not in block or 'dependencies: []' in block:
            targets[target_name] = []

# Targets to exclude from output (not needed for binary framework)
excluded_targets = {'_InstructionCounter', '_SwiftSyntaxTestSupport'}

# Output in format: TARGET_NAME:DEP1,DEP2,DEP3
for target, deps in sorted(targets.items()):
    if target in excluded_targets:
        continue
    # Filter to only include deps that are also targets (internal deps) and not excluded
    internal_deps = [d for d in deps if d in targets and d not in excluded_targets]
    print(f"{target}:{','.join(internal_deps)}")
PYTHON_SCRIPT
}

# Parse and store dependencies
DEPS_FILE="/tmp/swift_syntax_deps.txt"
parse_dependencies > "$DEPS_FILE"
echo "Parsed dependencies:"
cat "$DEPS_FILE"

# ============================================================================
# Build inner script (same as original)
# ============================================================================
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

# ============================================================================
# Build modules (same logic as original)
# ============================================================================
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
        cd $DDATA/Build/Intermediates.noindex/*.build/$CONFIG*/*$(echo $MODULE | sed s/^_//).build/Objects-normal &&
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
    zip -r9 --symlinks "$MODULE.xcframework.zip" "$MODULE.xcframework" >>../../zips.txt
OUTER
done

# Wait for all background jobs
wait

# ============================================================================
# Generate Package.swift with _Aggregation pattern
# ============================================================================
echo "Generating Package.swift..."

# Create Sources directory structure for Aggregation targets
OUTPUT_DIR=$(dirname "$OUTPUT_FILE")
SOURCES_DIR="$OUTPUT_DIR/Sources"
echo "Creating Aggregation target sources in $SOURCES_DIR..."

for MODULE in $MODULES; do
    TARGET_DIR="$SOURCES_DIR/${MODULE}_Aggregation"
    mkdir -p "$TARGET_DIR"
    # Create an empty Swift file for the aggregation target
    cat > "$TARGET_DIR/${MODULE}_Aggregation.swift" << SWIFT_EOF
// This file is intentionally empty.
// It exists only to satisfy SwiftPM's requirement for source files in targets.
// The actual implementation is provided by the binary target.
SWIFT_EOF
    echo "  Created $TARGET_DIR/${MODULE}_Aggregation.swift"
done

# Compute checksums and store them to file
CHECKSUMS_FILE="/tmp/swift_syntax_checksums.txt"
rm -f "$CHECKSUMS_FILE"
for MODULE in $MODULES; do
    if [ -f "$DEST/$MODULE.xcframework.zip" ]; then
        CHECKSUM=$(swift package compute-checksum "$DEST/$MODULE.xcframework.zip")
        echo "$MODULE:$CHECKSUM" >> "$CHECKSUMS_FILE"
        echo "$MODULE: $CHECKSUM"
    fi
done

# Helper function to get checksum for a module
get_checksum() {
    local module="$1"
    grep "^${module}:" "$CHECKSUMS_FILE" 2>/dev/null | cut -d: -f2
}

# Helper function to get dependencies for a module
get_deps() {
    local module="$1"
    grep "^${module}:" "$DEPS_FILE" 2>/dev/null | cut -d: -f2
}

# Define public products (matching swift-syntax's public API)
PUBLIC_PRODUCTS="SwiftBasicFormat SwiftCompilerPlugin SwiftDiagnostics SwiftIDEUtils SwiftIfConfig SwiftLexicalLookup SwiftOperators SwiftParser SwiftParserDiagnostics SwiftRefactor SwiftSyntax SwiftSyntaxBuilder SwiftSyntaxMacros SwiftSyntaxMacroExpansion SwiftSyntaxMacrosTestSupport SwiftSyntaxMacrosGenericTestSupport"

# Products with different public name vs internal target name
# Format: PUBLIC_NAME:TARGET_NAME
ALIASED_PRODUCTS="_SwiftCompilerPluginMessageHandling:SwiftCompilerPluginMessageHandling _SwiftLibraryPluginProvider:SwiftLibraryPluginProvider"

# Generate Package.swift
{
    cat << 'HEADER'
// swift-tools-version: 5.9

import PackageDescription

HEADER

    echo "let tag = \"$TAG\""
    echo ""
    echo "let package = Package("
    echo "    name: \"swift-syntax\","
    echo "    platforms: ["
    echo "        .iOS(.v13),"
    echo "        .macCatalyst(.v13),"
    echo "        .macOS(.v10_15),"
    echo "        .tvOS(.v13),"
    echo "        .watchOS(.v6),"
    echo "    ],"
    echo "    products: ["

    # Public products
    for PRODUCT in $PUBLIC_PRODUCTS; do
        echo "        .library(name: \"$PRODUCT\", targets: [\"${PRODUCT}_Aggregation\"]),"
    done

    # Aliased products
    for ALIAS in $ALIASED_PRODUCTS; do
        PUBLIC_NAME="${ALIAS%%:*}"
        TARGET_NAME="${ALIAS##*:}"
        echo "        .library(name: \"$PUBLIC_NAME\", targets: [\"${TARGET_NAME}_Aggregation\"]),"
    done

    echo "    ],"
    echo "    targets: ["

    # Generate targets for each module
    for MODULE in $MODULES; do
        DEPS=$(get_deps "$MODULE")
        CHECKSUM=$(get_checksum "$MODULE")

        echo "        // MARK: - $MODULE"
        echo "        .target("
        echo "            name: \"${MODULE}_Aggregation\","

        if [ -z "$DEPS" ]; then
            echo "            dependencies: [.target(name: \"$MODULE\")]"
        else
            echo "            dependencies: ["
            echo "                .target(name: \"$MODULE\"),"
            # Split deps by comma
            echo "$DEPS" | tr ',' '\n' | while read -r DEP; do
                if [ -n "$DEP" ]; then
                    echo "                \"${DEP}_Aggregation\","
                fi
            done
            echo "            ]"
        fi
        echo "        ),"

        # Binary target
        if [ "$MODE" == "local" ]; then
            echo "        .binaryTarget(name: \"$MODULE\", path: tag + \"/$MODULE.xcframework.zip\"),"
        else
            URL="$BASE_URL/$TAG/$MODULE.xcframework.zip"
            echo "        .binaryTarget("
            echo "            name: \"$MODULE\","
            echo "            url: \"$URL\","
            echo "            checksum: \"$CHECKSUM\""
            echo "        ),"
        fi
        echo ""
    done

    echo "    ]"
    echo ")"
} > "$OUTPUT_FILE"

echo ""
echo "Build complete!"
echo "Generated: $OUTPUT_FILE"
echo "XCFrameworks: $DEST/"
