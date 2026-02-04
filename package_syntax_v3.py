#!/usr/bin/env python3
"""
DIY rebuild of all binary framework zip files from Apple source.
Generates Package.swift with _Aggregation target pattern.

Usage:
    python3 package_syntax_v3.py [OPTIONS]

Options:
    --repo URL          Repository URL (default: https://github.com/apple/swift-syntax)
    --tag VERSION       Tag/version to build (default: 601.0.1)
    --platforms LIST    Platforms to build, comma-separated (default: macOS)
                        Examples: "macOS" "macOS,iOS" "macOS,iOS,iOS_Simulator"
    --mode MODE         "local" for local paths, "remote" for URLs (default: local)
    --base-url URL      Base URL for remote xcframeworks (required if mode=remote)
    --output FILE       Output Package.swift path (default: ./Package.swift)
    --help              Show this help message

Examples:
    python3 package_syntax_v3.py --tag 601.0.1 --platforms macOS
    python3 package_syntax_v3.py --tag 601.0.1 --mode remote --base-url "https://example.com/frameworks"
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set


class SwiftSyntaxBuilder:
    """Builder for swift-syntax binary frameworks."""

    # Modules to exclude from build
    EXCLUDED_TARGETS: Set[str] = {'_InstructionCounter', '_SwiftSyntaxTestSupport'}

    # Public products matching swift-syntax's public API
    PUBLIC_PRODUCTS: List[str] = [
        "SwiftBasicFormat",
        "SwiftCompilerPlugin",
        "SwiftDiagnostics",
        "SwiftIDEUtils",
        "SwiftIfConfig",
        "SwiftLexicalLookup",
        "SwiftOperators",
        "SwiftParser",
        "SwiftParserDiagnostics",
        "SwiftRefactor",
        "SwiftSyntax",
        "SwiftSyntaxBuilder",
        "SwiftSyntaxMacros",
        "SwiftSyntaxMacroExpansion",
        "SwiftSyntaxMacrosTestSupport",
        "SwiftSyntaxMacrosGenericTestSupport",
    ]

    # Products with different public name vs internal target name
    ALIASED_PRODUCTS: Dict[str, str] = {
        "_SwiftCompilerPluginMessageHandling": "SwiftCompilerPluginMessageHandling",
        "_SwiftLibraryPluginProvider": "SwiftLibraryPluginProvider",
    }

    def __init__(
        self,
        repo: str,
        tag: str,
        platforms: List[str],
        mode: str,
        base_url: Optional[str],
        output_file: str,
        parallel_builds: int = 4,
        config: str = "Release",
        conditions: str = "RESILIENT_LIBRARIES",
    ):
        self.repo = repo
        self.repo_name = os.path.basename(repo)
        self.tag = tag
        self.platforms = platforms
        self.mode = mode
        self.base_url = base_url
        self.output_file = Path(output_file).resolve()
        self.parallel_builds = parallel_builds
        self.config = config
        self.conditions = conditions

        self.dest = Path.cwd() / tag
        self.source = Path("/tmp") / self.repo_name
        self.xcoded = self._get_xcode_path()

        self.modules: List[str] = []
        self.dependencies: Dict[str, List[str]] = {}
        self.checksums: Dict[str, str] = {}

    def _get_xcode_path(self) -> str:
        """Get Xcode developer path."""
        result = subprocess.run(
            ["xcode-select", "-p"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()

    def _run_command(self, cmd: List[str], cwd: Optional[Path] = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run a shell command."""
        print(f"+ {' '.join(cmd)}")
        return subprocess.run(cmd, cwd=cwd, check=check)

    def _run_command_output(self, cmd: List[str], cwd: Optional[Path] = None) -> str:
        """Run a shell command and return output."""
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def clone_repo(self):
        """Clone the repository if needed."""
        if not self.source.exists():
            print(f"Cloning {self.repo}...")
            self._run_command(["git", "clone", self.repo, str(self.source)])

        self.dest.mkdir(parents=True, exist_ok=True)
        os.chdir(self.source)
        (self.source / "archives").mkdir(exist_ok=True)

        # Stash and checkout
        self._run_command(["git", "stash"], check=False)
        self._run_command(["git", "checkout", self.tag])

    def patch_package_swift(self):
        """Patch Package.swift to expose additional internal targets as library products."""
        package_file = self.source / "Package.swift"
        content = package_file.read_text()

        # Check if already patched
        if '_SwiftLibraryPluginProviderCShims' in content and '.library(name: "_SwiftLibraryPluginProviderCShims"' in content:
            print("Package.swift already patched, skipping...")
            return

        # Find the line to insert after
        insert_after = '.library(name: "_SwiftLibraryPluginProvider", targets:'

        additional_products = '''
    .library(name: "_SwiftLibraryPluginProviderCShims", targets: ["_SwiftLibraryPluginProviderCShims"]),
    .library(name: "_SwiftSyntaxCShims", targets: ["_SwiftSyntaxCShims"]),
    .library(name: "_SwiftSyntaxGenericTestSupport", targets: ["_SwiftSyntaxGenericTestSupport"]),
    .library(name: "SwiftCompilerPluginMessageHandling", targets: ["SwiftCompilerPluginMessageHandling"]),
    .library(name: "SwiftLibraryPluginProvider", targets: ["SwiftLibraryPluginProvider"]),
    .library(name: "SwiftSyntax509", targets: ["SwiftSyntax509"]),
    .library(name: "SwiftSyntax510", targets: ["SwiftSyntax510"]),
    .library(name: "SwiftSyntax600", targets: ["SwiftSyntax600"]),
    .library(name: "SwiftSyntax601", targets: ["SwiftSyntax601"]),'''

        # Find the position and insert
        pos = content.find(insert_after)
        if pos == -1:
            raise ValueError(f"Could not find '{insert_after}' in Package.swift")

        # Find the end of that line
        end_of_line = content.find('\n', pos)

        new_content = content[:end_of_line + 1] + additional_products + content[end_of_line + 1:]
        package_file.write_text(new_content)
        print("Patched Package.swift")

    def extract_modules(self):
        """Extract module names from patched Package.swift."""
        package_file = self.source / "Package.swift"
        content = package_file.read_text()

        # Find all .library(name: "XXX" entries
        pattern = r'\.library\(name:\s*"([^"]+)"'
        matches = re.findall(pattern, content)

        # Filter out _SwiftSyntaxDynamic
        self.modules = [m for m in matches if m != '_SwiftSyntaxDynamic']
        print(f"Modules to build: {' '.join(self.modules)}")

    def parse_dependencies(self):
        """Parse dependencies from swift-syntax Package.swift."""
        package_file = self.source / "Package.swift"
        content = package_file.read_text()

        # Pattern to match .target definitions with dependencies
        target_pattern = r'\.target\s*\(\s*name:\s*"([^"]+)"[^)]*?dependencies:\s*\[([^\]]*)\]'

        targets: Dict[str, List[str]] = {}

        for match in re.finditer(target_pattern, content, re.DOTALL):
            target_name = match.group(1)
            deps_str = match.group(2)

            # Parse string dependencies
            deps = re.findall(r'"([^"]+)"', deps_str)

            # Filter out test-only dependencies
            deps = [d for d in deps
                    if not d.startswith('_SwiftSyntaxTestSupport')
                    and not d.startswith('_InstructionCounter')
                    and not d.endswith('Test')]

            targets[target_name] = deps

        # Find targets without dependencies
        no_dep_pattern = r'\.target\s*\(\s*name:\s*"([^"]+)"[^)]*?\)'
        for match in re.finditer(no_dep_pattern, content, re.DOTALL):
            target_name = match.group(1)
            if target_name not in targets:
                block = content[match.start():match.end()]
                if 'dependencies:' not in block or 'dependencies: []' in block:
                    targets[target_name] = []

        # Filter and store dependencies
        for target, deps in targets.items():
            if target in self.EXCLUDED_TARGETS:
                continue
            # Filter to only include internal deps that are not excluded
            internal_deps = [d for d in deps if d in targets and d not in self.EXCLUDED_TARGETS]
            self.dependencies[target] = internal_deps

        print("Parsed dependencies:")
        for target, deps in sorted(self.dependencies.items()):
            print(f"  {target}: {','.join(deps) if deps else '(none)'}")

    def build_module_for_platform(self, module: str, platform: str) -> bool:
        """Build a single module for a single platform."""
        ddata = self.source / f"build.{platform}"
        dest_platform = platform.replace('_', ' ')

        xcodebuild_cmd = [
            f"{self.xcoded}/usr/bin/xcodebuild",
            "-scheme", module,
            "-quiet",
            "-configuration", self.config,
            "-destination", f"generic/platform={dest_platform}",
            "-archivePath", str(self.source / "archives" / f"{module}-{platform}.xcarchive"),
            "-derivedDataPath", str(ddata),
            "SKIP_INSTALL=NO",
            "BUILD_LIBRARY_FOR_DISTRIBUTION=YES",
            "SWIFT_SERIALIZE_DEBUGGING_OPTIONS=NO",
            f"SWIFT_ACTIVE_COMPILATION_CONDITIONS={self.conditions}",
            "SWIFT_VERSION=5",
        ]

        if platform == "macOS":
            xcodebuild_cmd.append("ARCHS=arm64 arm64e x86_64")

        try:
            subprocess.run(xcodebuild_cmd, cwd=self.source, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"Failed to build {module} for {platform}: {e}")
            return False

    def create_xcframework(self, module: str):
        """Create xcframework from built libraries."""
        libs = []

        for platform in self.platforms:
            ddata = self.source / f"build.{platform}"
            lib_path = ddata / f"lib{module}.a"

            # Remove old .a files
            for old_lib in ddata.glob(f"lib{module}*.a"):
                old_lib.unlink()

            # Find the build directory
            module_name_pattern = module.lstrip('_')
            intermediates = ddata / "Build" / "Intermediates.noindex"

            build_dir = None
            for config_dir in intermediates.glob("*.build"):
                for release_dir in config_dir.glob(f"{self.config}*"):
                    for target_dir in release_dir.glob(f"*{module_name_pattern}.build"):
                        objects_dir = target_dir / "Objects-normal"
                        if objects_dir.exists():
                            build_dir = objects_dir
                            break

            if not build_dir:
                print(f"Warning: Could not find build directory for {module} on {platform}")
                continue

            # Create .a files for each architecture
            arch_libs = []
            for arch_dir in build_dir.iterdir():
                if arch_dir.is_dir():
                    arch = arch_dir.name
                    arch_lib = ddata / f"lib{module}.{arch}.a"

                    # Get all .o files
                    o_files = list(arch_dir.glob("*.o"))
                    if o_files:
                        subprocess.run(
                            ["ar", "qv", str(arch_lib)] + [str(f) for f in o_files],
                            check=True,
                            capture_output=True
                        )
                        subprocess.run(["ranlib", str(arch_lib)], check=True, capture_output=True)
                        arch_libs.append(str(arch_lib))

            # Create fat library with lipo
            if arch_libs:
                subprocess.run(
                    ["lipo", "-create"] + arch_libs + ["-output", str(lib_path)],
                    check=True,
                    capture_output=True
                )
                libs.extend(["-library", str(lib_path)])

        # Create xcframework
        xcframework_path = self.dest / f"{module}.xcframework"
        if xcframework_path.exists():
            shutil.rmtree(xcframework_path)

        if libs:
            subprocess.run(
                [f"{self.xcoded}/usr/bin/xcodebuild", "-create-xcframework"] + libs +
                ["-output", str(xcframework_path)],
                check=True,
                capture_output=True
            )

        # Copy swift modules
        self._copy_swift_modules(module, xcframework_path)

        # Create zip
        self._create_zip(module, xcframework_path)

    def _copy_swift_modules(self, module: str, xcframework_path: Path):
        """Copy swift modules to xcframework."""
        if not xcframework_path.exists():
            return

        for variant in xcframework_path.iterdir():
            if variant.name == "Info.plist" or not variant.is_dir():
                continue

            if variant.name.startswith("macos-"):
                products_dir = self.source / "build.macOS" / "Build" / "Products" / "Release"
            elif variant.name == "ios-arm64":
                products_dir = self.source / "build.iOS" / "Build" / "Products" / "Release-iphoneos"
            elif "simulator" in variant.name and variant.name.startswith("ios-"):
                products_dir = self.source / "build.iOS_Simulator" / "Build" / "Products" / "Release-iphonesimulator"
            elif "simulator" in variant.name and variant.name.startswith("tvos-"):
                products_dir = self.source / "build.tvOS_Simulator" / "Build" / "Products" / "Release-appletvsimulator"
            else:
                continue

            # Try to copy swift module
            swift_module = products_dir / f"{module}.swiftmodule"
            if swift_module.exists():
                dest = variant / f"{module}.swiftmodule"
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(swift_module, dest)
            else:
                # Fallback to SwiftSyntax509.swiftmodule
                fallback = products_dir / "SwiftSyntax509.swiftmodule"
                if fallback.exists():
                    dest = variant / f"{module}.swiftmodule"
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(fallback, dest)

    def _create_zip(self, module: str, xcframework_path: Path):
        """Create zip file and optionally sign the xcframework."""
        if not xcframework_path.exists():
            return

        # Remove .swiftmodule files inside the xcframework
        for swiftmodule in xcframework_path.glob("*/*/*.swiftmodule"):
            if swiftmodule.is_file():
                swiftmodule.unlink()

        # Code sign (optional, may fail if no valid identity)
        try:
            subprocess.run(
                ["codesign", "-f", "--timestamp", "-s",
                 "Apple Development: JieHui Lai (4ZZALU97YZ)", str(xcframework_path)],
                check=True,
                capture_output=True
            )
        except subprocess.CalledProcessError:
            print(f"Warning: Could not sign {module}.xcframework")

        # Create zip
        zip_path = self.dest / f"{module}.xcframework.zip"
        if zip_path.exists():
            zip_path.unlink()

        subprocess.run(
            ["zip", "-r9", "--symlinks", str(zip_path), f"{module}.xcframework"],
            cwd=self.dest,
            check=True,
            capture_output=True
        )

    def build_all_modules(self):
        """Build all modules."""
        for module in self.modules:
            print(f"\n{'='*60}")
            print(f"Building {module}...")
            print('='*60)

            # Build for all platforms in parallel
            with ThreadPoolExecutor(max_workers=self.parallel_builds) as executor:
                futures = {
                    executor.submit(self.build_module_for_platform, module, platform): platform
                    for platform in self.platforms
                }
                for future in as_completed(futures):
                    platform = futures[future]
                    try:
                        success = future.result()
                        if success:
                            print(f"  ✓ Built {module} for {platform}")
                        else:
                            print(f"  ✗ Failed {module} for {platform}")
                    except Exception as e:
                        print(f"  ✗ Error building {module} for {platform}: {e}")

            # Create xcframework
            self.create_xcframework(module)
            print(f"  ✓ Created {module}.xcframework.zip")

    def compute_checksums(self):
        """Compute checksums for all xcframework zips."""
        print("\nComputing checksums...")
        for module in self.modules:
            zip_path = self.dest / f"{module}.xcframework.zip"
            if zip_path.exists():
                result = subprocess.run(
                    ["swift", "package", "compute-checksum", str(zip_path)],
                    capture_output=True,
                    text=True,
                    check=True
                )
                checksum = result.stdout.strip()
                self.checksums[module] = checksum
                print(f"  {module}: {checksum}")

    def create_sources_directory(self):
        """Create Sources directory structure for Aggregation targets."""
        output_dir = self.output_file.parent
        sources_dir = output_dir / "Sources"

        print(f"\nCreating Aggregation target sources in {sources_dir}...")

        for module in self.modules:
            target_dir = sources_dir / f"{module}_Aggregation"
            target_dir.mkdir(parents=True, exist_ok=True)

            swift_file = target_dir / f"{module}_Aggregation.swift"
            swift_file.write_text(
                "// This file is intentionally empty.\n"
                "// It exists only to satisfy SwiftPM's requirement for source files in targets.\n"
                "// The actual implementation is provided by the binary target.\n"
            )
            print(f"  Created {swift_file}")

    def generate_package_swift(self):
        """Generate Package.swift with _Aggregation pattern."""
        print(f"\nGenerating {self.output_file}...")

        lines = [
            "// swift-tools-version: 5.9",
            "",
            "import PackageDescription",
            "",
            f'let tag = "{self.tag}"',
            "",
            "let package = Package(",
            '    name: "swift-syntax",',
            "    platforms: [",
            "        .iOS(.v13),",
            "        .macCatalyst(.v13),",
            "        .macOS(.v10_15),",
            "        .tvOS(.v13),",
            "        .watchOS(.v6),",
            "    ],",
            "    products: [",
        ]

        # Public products
        for product in self.PUBLIC_PRODUCTS:
            lines.append(f'        .library(name: "{product}", targets: ["{product}_Aggregation"]),')

        # Aliased products
        for public_name, target_name in self.ALIASED_PRODUCTS.items():
            lines.append(f'        .library(name: "{public_name}", targets: ["{target_name}_Aggregation"]),')

        lines.append("    ],")
        lines.append("    targets: [")

        # Generate targets for each module
        for module in self.modules:
            deps = self.dependencies.get(module, [])
            checksum = self.checksums.get(module, "")

            lines.append(f"        // MARK: - {module}")
            lines.append("        .target(")
            lines.append(f'            name: "{module}_Aggregation",')

            if not deps:
                lines.append(f'            dependencies: [.target(name: "{module}")]')
            else:
                lines.append("            dependencies: [")
                lines.append(f'                .target(name: "{module}"),')
                for dep in deps:
                    lines.append(f'                "{dep}_Aggregation",')
                lines.append("            ]")

            lines.append("        ),")

            # Binary target
            if self.mode == "local":
                lines.append(f'        .binaryTarget(name: "{module}", path: tag + "/{module}.xcframework.zip"),')
            else:
                url = f"{self.base_url}/{self.tag}/{module}.xcframework.zip"
                lines.append("        .binaryTarget(")
                lines.append(f'            name: "{module}",')
                lines.append(f'            url: "{url}",')
                lines.append(f'            checksum: "{checksum}"')
                lines.append("        ),")

            lines.append("")

        lines.append("    ]")
        lines.append(")")
        lines.append("")

        self.output_file.write_text('\n'.join(lines))
        print(f"Generated: {self.output_file}")

    def run(self):
        """Run the complete build process."""
        print(f"Building swift-syntax {self.tag}")
        print(f"Platforms: {', '.join(self.platforms)}")
        print(f"Mode: {self.mode}")
        print(f"Output: {self.output_file}")
        print()

        self.clone_repo()
        self.patch_package_swift()
        self.extract_modules()
        self.parse_dependencies()
        self.build_all_modules()
        self.compute_checksums()
        self.create_sources_directory()
        self.generate_package_swift()

        print()
        print("=" * 60)
        print("Build complete!")
        print(f"Generated: {self.output_file}")
        print(f"XCFrameworks: {self.dest}/")
        print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Build swift-syntax binary frameworks and generate Package.swift",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    %(prog)s --tag 601.0.1 --platforms macOS
    %(prog)s --tag 601.0.1 --platforms macOS,iOS,iOS_Simulator
    %(prog)s --tag 601.0.1 --mode remote --base-url "https://example.com/frameworks"
        """
    )

    parser.add_argument(
        "--repo",
        default="https://github.com/apple/swift-syntax",
        help="Repository URL (default: %(default)s)"
    )
    parser.add_argument(
        "--tag",
        default="601.0.1",
        help="Tag/version to build (default: %(default)s)"
    )
    parser.add_argument(
        "--platforms",
        default="macOS",
        help="Platforms to build, comma-separated (default: %(default)s)"
    )
    parser.add_argument(
        "--mode",
        choices=["local", "remote"],
        default="local",
        help="Output mode: local paths or remote URLs (default: %(default)s)"
    )
    parser.add_argument(
        "--base-url",
        help="Base URL for remote xcframeworks (required if mode=remote)"
    )
    parser.add_argument(
        "--output",
        default="./Package.swift",
        help="Output Package.swift path (default: %(default)s)"
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=4,
        help="Number of parallel builds (default: %(default)s)"
    )

    args = parser.parse_args()

    # Validate arguments
    if args.mode == "remote" and not args.base_url:
        parser.error("--base-url is required when --mode is 'remote'")

    # Parse platforms
    platforms = [p.strip() for p in args.platforms.split(",")]

    # Create builder and run
    builder = SwiftSyntaxBuilder(
        repo=args.repo,
        tag=args.tag,
        platforms=platforms,
        mode=args.mode,
        base_url=args.base_url,
        output_file=args.output,
        parallel_builds=args.parallel,
    )

    try:
        builder.run()
    except KeyboardInterrupt:
        print("\nBuild cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
