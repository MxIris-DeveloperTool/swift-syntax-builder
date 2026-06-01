#!/usr/bin/env python3
"""
DIY rebuild of all binary framework zip files from Apple source.
Generates Package.swift with _Aggregation target pattern.

Usage:
    python3 package_syntax.py [OPTIONS]

Options:
    --repo URL          Repository URL (default: https://github.com/apple/swift-syntax)
    --tag VERSION       Tag/version to build (default: 601.0.1)
    --platforms LIST    Platforms to build, comma-separated (default: macOS).
                        Valid names: macOS, iOS, iOS_Simulator, tvOS, tvOS_Simulator,
                        watchOS, watchOS_Simulator, visionOS, visionOS_Simulator.
    --all-platforms     Build for every supported platform (overrides --platforms).
    --mode MODE         "local" for local paths, "remote" for URLs (default: local)
    --base-url URL      Base URL for remote xcframeworks (required if mode=remote
                        and --publish is not set)
    --output FILE       Output Package.swift path (default: ./Package.swift)
    --publish           Upload built zips to a GitHub Release (requires `gh` CLI)
    --release-repo R    Target repo for --publish in OWNER/REPO form
                        (default: detect from current git origin)
    --release-title T   Release title for --publish (default: tag)
    --release-notes N   Release notes body for --publish
    --publish-branch    Push Package.swift + Sources/ to a release branch
                        (orphan branch, force-pushed)
    --branch-name N     Branch name for --publish-branch (default: release/<tag>)
    --branch-repo R     Target repo for --publish-branch (default: --release-repo
                        if set, else detect from current git origin)
    --lower-platforms   Lower deployment targets to macOS 10.13 / iOS 12 /
                        tvOS 12 / watchOS 4 (macCatalyst 13). Patches upstream
                        sources to compile under the lower minimums. Off by
                        default — upstream macOS 10.15 / iOS 13 / tvOS 13 /
                        watchOS 6 are kept and no swift-syntax files are touched.
    --release-tag T     GitHub Release tag (default: --tag, or
                        `<tag>-lower-platforms` when --lower-platforms is set).
                        Lets one upstream version be published twice — once
                        with original deployment targets, once with lowered.
    --help              Show this help message

Examples:
    python3 package_syntax.py --tag 601.0.1 --platforms macOS
    python3 package_syntax.py --tag 603.0.1 --all-platforms
    python3 package_syntax.py --tag 603.0.1 --mode remote --base-url "https://example.com/frameworks"
    python3 package_syntax.py --tag 603.0.1 --all-platforms --publish --publish-branch \\
        --mode remote --release-repo MxIris-DeveloperTool/swift-syntax-builder
    python3 package_syntax.py --tag 603.0.1 --all-platforms --lower-platforms
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


class SwiftSyntaxBuilder:
    """Builder for swift-syntax binary frameworks."""

    # Modules to exclude from build. `SwiftSyntax-all` is swift-syntax's meta
    # umbrella target that depends on every non-test target — it exists only
    # for `swift build`/CI purposes and shouldn't be shipped as a product.
    EXCLUDED_TARGETS: Set[str] = {
        '_InstructionCounter',
        '_SwiftSyntaxTestSupport',
        'SwiftSyntax-all',
    }

    # Public products matching swift-syntax's public API.
    # Products that don't exist in the selected tag are filtered out at generation time.
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
        "SwiftWarningControl",
    ]

    # Products with different public name vs internal target name
    ALIASED_PRODUCTS: Dict[str, str] = {
        "_SwiftCompilerPluginMessageHandling": "SwiftCompilerPluginMessageHandling",
        "_SwiftLibraryPluginProvider": "SwiftLibraryPluginProvider",
    }

    # Mapping: platform name -> (derived-data dir name, Products subdirectory).
    # Platform names use underscores so they can be passed to xcodebuild via
    # `platform.replace('_', ' ')` (e.g. "iOS_Simulator" -> "iOS Simulator").
    PLATFORM_BUILD_DIRS: Dict[str, Tuple[str, str]] = {
        "macOS": ("build.macOS", "Release"),
        "macCatalyst": ("build.macCatalyst", "Release-maccatalyst"),
        "iOS": ("build.iOS", "Release-iphoneos"),
        "iOS_Simulator": ("build.iOS_Simulator", "Release-iphonesimulator"),
        "tvOS": ("build.tvOS", "Release-appletvos"),
        "tvOS_Simulator": ("build.tvOS_Simulator", "Release-appletvsimulator"),
        "watchOS": ("build.watchOS", "Release-watchos"),
        "watchOS_Simulator": ("build.watchOS_Simulator", "Release-watchsimulator"),
        "visionOS": ("build.visionOS", "Release-xros"),
        "visionOS_Simulator": ("build.visionOS_Simulator", "Release-xrsimulator"),
    }

    # Platforms whose xcodebuild destination string isn't the default
    # "generic/platform=<name>" form. Mac Catalyst is built by selecting the
    # macOS platform with a `Mac Catalyst` variant.
    _DESTINATION_OVERRIDES: Dict[str, str] = {
        "macCatalyst": "generic/platform=macOS,variant=Mac Catalyst",
    }

    ALL_PLATFORMS: List[str] = list(PLATFORM_BUILD_DIRS.keys())

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
        publish: bool = False,
        release_repo: Optional[str] = None,
        release_title: Optional[str] = None,
        release_notes: Optional[str] = None,
        publish_branch: bool = False,
        branch_name: Optional[str] = None,
        branch_repo: Optional[str] = None,
        lower_platforms: bool = False,
        release_tag: Optional[str] = None,
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
        self.publish = publish
        self.release_repo = release_repo
        self.release_title = release_title
        self.release_notes = release_notes
        self.publish_branch_flag = publish_branch
        self.branch_repo = branch_repo
        self.lower_platforms = lower_platforms
        # `release_tag` is the tag used for the GitHub Release and the
        # release-branch name; it can differ from the upstream build tag
        # so a single upstream version can be published twice — once with
        # the original deployment targets and once with lowered ones.
        self.release_tag = release_tag or (
            f"{tag}-lower-platforms" if lower_platforms else tag
        )
        self.branch_name = branch_name or f"release/{self.release_tag}"

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
        """Patch Package.swift to expose every non-test internal target as a library.

        The set of exposed targets is computed dynamically: any `.target` /
        `.macro` / `.executableTarget` whose name is not already used as a
        library name (and isn't excluded) gets a same-name library appended.
        Idempotent: re-runs add only the libraries still missing.
        """
        package_file = self.source / "Package.swift"
        content = package_file.read_text()

        existing_lib_names = set(re.findall(r'\.library\(\s*name:\s*"([^"]+)"', content))

        # `\.target\(` does not match `.testTarget(` (different spelling), so
        # we don't need a special exclusion for test targets.
        target_re = re.compile(r'\.(?:target|macro|executableTarget)\(\s*name:\s*"([^"]+)"')
        all_targets = set(target_re.findall(content))

        targets_to_expose = sorted(
            target
            for target in all_targets
            if target not in self.EXCLUDED_TARGETS
            and target not in existing_lib_names
        )

        if not targets_to_expose:
            print("Package.swift already exposes all needed libraries, skipping patch...")
            return

        # `_SwiftLibraryPluginProvider` is a stable anchor across 5xx-6xx releases.
        insert_after = '.library(name: "_SwiftLibraryPluginProvider", targets:'
        pos = content.find(insert_after)
        if pos == -1:
            raise ValueError(f"Could not find '{insert_after}' in Package.swift")

        end_of_line = content.find('\n', pos)

        additions = '\n'.join(
            f'    .library(name: "{name}", targets: ["{name}"]),'
            for name in targets_to_expose
        ) + '\n'
        new_content = content[:end_of_line + 1] + additions + content[end_of_line + 1:]
        package_file.write_text(new_content)
        print(f"Patched Package.swift, exposed {len(targets_to_expose)} libraries: {targets_to_expose}")

    # Upstream swift-syntax 603.0.1 deployment targets, kept in sync with
    # the values declared in apple/swift-syntax's Package.swift. Used as
    # the default when --lower-platforms is not passed.
    UPSTREAM_PLATFORM_LINES: List[str] = [
        ".macOS(.v10_15)",
        ".iOS(.v13)",
        ".tvOS(.v13)",
        ".watchOS(.v6)",
        ".macCatalyst(.v13)",
    ]

    # Lower-platform deployment targets used when --lower-platforms is set.
    # macOS arm64 binaries still have a hard 11.0 floor at the Mach-O level,
    # but x86_64 slices drop all the way to 10.13, so consumers can declare
    # 10.13 deployment targets and pick the appropriate slice at runtime.
    LOWERED_PLATFORM_LINES: List[str] = [
        ".macOS(.v10_13)",
        ".iOS(.v12)",
        ".tvOS(.v12)",
        ".watchOS(.v4)",
        ".macCatalyst(.v13)",
    ]

    def lower_upstream_platforms(self):
        """Rewrite upstream Package.swift `platforms` block to lower minimums.

        swift-syntax declares macOS 10.15 / iOS 13 / tvOS 13 / watchOS 6 in
        its Package.swift. Macros are compile-time consumed (never linked
        into the final app binary), so the deployment target inherited by
        consumers can be much lower. We rewrite the block to the lowest
        SwiftPM tools-version-5.9 defaults that still compile.

        Idempotent: bails out if the block is already at our targets.
        """
        package_file = self.source / "Package.swift"
        content = package_file.read_text()

        block_re = re.compile(r'platforms:\s*\[(.*?)\]', re.DOTALL)
        match = block_re.search(content)
        if not match:
            print("  No platforms: block found in upstream Package.swift, skipping...")
            return

        existing_block = match.group(1)
        if all(line in existing_block for line in self.LOWERED_PLATFORM_LINES):
            print("Upstream Package.swift platforms already lowered, skipping...")
            return

        indent = "    "
        new_block = "platforms: [\n" + "\n".join(
            f"{indent}{line},"
            for line in self.LOWERED_PLATFORM_LINES
        ) + f"\n{indent[:-2]}]"
        new_content = content[:match.start()] + new_block + content[match.end():]
        package_file.write_text(new_content)
        print(f"Lowered upstream platforms to: {', '.join(self.LOWERED_PLATFORM_LINES)}")

    # Source-level compatibility patches required when the deployment target
    # is lowered below macOS 10.15. Each entry: (relative path, old text,
    # new text). Patches are applied in order; missing files / mismatched
    # text are skipped with a warning so the script keeps working on tags
    # where the upstream source has already moved on.
    LOWER_PLATFORM_SOURCE_PATCHES: List[Tuple[str, str, str]] = [
        (
            "Sources/_SwiftSyntaxGenericTestSupport/AssertEqualWithDiff.swift",
            "  let stringComparison: String\n"
            "\n"
            "  // Use `CollectionDifference` on supported platforms to get `diff`-like line-based output. On\n"
            "  // older platforms, fall back to simple string comparison.\n"
            "  let actualLines = actual.split(separator: \"\\n\", omittingEmptySubsequences: false)\n"
            "  let expectedLines = expected.split(separator: \"\\n\", omittingEmptySubsequences: false)\n"
            "\n"
            "  let difference = actualLines.difference(from: expectedLines)\n"
            "\n"
            "  var result = \"\"\n"
            "\n"
            "  var insertions = [Int: Substring]()\n"
            "  var removals = [Int: Substring]()\n"
            "\n"
            "  for change in difference {\n"
            "    switch change {\n"
            "    case .insert(let offset, let element, _):\n"
            "      insertions[offset] = element\n"
            "    case .remove(let offset, let element, _):\n"
            "      removals[offset] = element\n"
            "    }\n"
            "  }\n"
            "\n"
            "  var expectedLine = 0\n"
            "  var actualLine = 0\n"
            "\n"
            "  while expectedLine < expectedLines.count || actualLine < actualLines.count {\n"
            "    if let removal = removals[expectedLine] {\n"
            "      result += \"–\\(removal)\\n\"\n"
            "      expectedLine += 1\n"
            "    } else if let insertion = insertions[actualLine] {\n"
            "      result += \"+\\(insertion)\\n\"\n"
            "      actualLine += 1\n"
            "    } else {\n"
            "      result += \" \\(expectedLines[expectedLine])\\n\"\n"
            "      expectedLine += 1\n"
            "      actualLine += 1\n"
            "    }\n"
            "  }\n"
            "\n"
            "  stringComparison = result",
            "  let stringComparison: String\n"
            "\n"
            "  // Use `CollectionDifference` on supported platforms to get `diff`-like line-based output. On\n"
            "  // older platforms, fall back to simple string comparison.\n"
            "  if #available(macOS 10.15, iOS 13.0, tvOS 13.0, watchOS 6.0, *) {\n"
            "    let actualLines = actual.split(separator: \"\\n\", omittingEmptySubsequences: false)\n"
            "    let expectedLines = expected.split(separator: \"\\n\", omittingEmptySubsequences: false)\n"
            "\n"
            "    let difference = actualLines.difference(from: expectedLines)\n"
            "\n"
            "    var result = \"\"\n"
            "\n"
            "    var insertions = [Int: Substring]()\n"
            "    var removals = [Int: Substring]()\n"
            "\n"
            "    for change in difference {\n"
            "      switch change {\n"
            "      case .insert(let offset, let element, _):\n"
            "        insertions[offset] = element\n"
            "      case .remove(let offset, let element, _):\n"
            "        removals[offset] = element\n"
            "      }\n"
            "    }\n"
            "\n"
            "    var expectedLine = 0\n"
            "    var actualLine = 0\n"
            "\n"
            "    while expectedLine < expectedLines.count || actualLine < actualLines.count {\n"
            "      if let removal = removals[expectedLine] {\n"
            "        result += \"–\\(removal)\\n\"\n"
            "        expectedLine += 1\n"
            "      } else if let insertion = insertions[actualLine] {\n"
            "        result += \"+\\(insertion)\\n\"\n"
            "        actualLine += 1\n"
            "      } else {\n"
            "        result += \" \\(expectedLines[expectedLine])\\n\"\n"
            "        expectedLine += 1\n"
            "        actualLine += 1\n"
            "      }\n"
            "    }\n"
            "\n"
            "    stringComparison = result\n"
            "  } else {\n"
            "    stringComparison = \"\"\"\n"
            "      Actual:\n"
            "      \\(actual)\n"
            "      Expected:\n"
            "      \\(expected)\n"
            "      \"\"\"\n"
            "  }",
        ),
        (
            "Sources/SwiftRefactor/PackageManifest/StringUtils.swift",
            "    // Combine the characters as a string again and return it.\n"
            "    // FIXME: We should only construct a new string if anything changed.\n"
            "    // FIXME: There doesn't seem to be a way to create a string from an\n"
            "    //        array of Unicode scalars; but there must be a better way.\n"
            "    return String(decoding: mangledUnichars.flatMap { $0.utf8 }, as: UTF8.self)",
            "    // Combine the characters as a string again and return it.\n"
            "    // FIXME: We should only construct a new string if anything changed.\n"
            "    return mangledUnichars.reduce(into: \"\") { $0.unicodeScalars.append($1) }",
        ),
    ]

    def patch_upstream_source_compat(self):
        """Apply source-level fixups so swift-syntax compiles below macOS 10.15.

        603.0.1 has two call sites that use macOS 10.15-only Standard
        Library APIs (`Collection.difference(from:)` and
        `Unicode.Scalar.utf8`). Patches are idempotent: if the patched text
        is already present, the entry is skipped.
        """
        for rel_path, old_text, new_text in self.LOWER_PLATFORM_SOURCE_PATCHES:
            file_path = self.source / rel_path
            if not file_path.exists():
                print(f"  Skip patch {rel_path}: file not present (probably newer tag)")
                continue
            content = file_path.read_text()
            if new_text in content:
                print(f"  Skip patch {rel_path}: already patched")
                continue
            if old_text not in content:
                print(f"  Warning: patch target not found in {rel_path}; upstream source may have changed")
                continue
            file_path.write_text(content.replace(old_text, new_text))
            print(f"  Patched {rel_path} for lower deployment target")

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
        destination = self._DESTINATION_OVERRIDES.get(
            platform, f"generic/platform={platform.replace('_', ' ')}"
        )

        xcodebuild_cmd = [
            f"{self.xcoded}/usr/bin/xcodebuild",
            "-scheme", module,
            "-quiet",
            "-configuration", self.config,
            "-destination", destination,
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
        elif platform == "macCatalyst":
            # Mac Catalyst doesn't support arm64e. Forcing the variant avoids
            # a "Designed for iPad" build slipping through.
            xcodebuild_cmd += [
                "ARCHS=arm64 x86_64",
                "SUPPORTS_MACCATALYST=YES",
                "SUPPORTS_MAC_DESIGNED_FOR_IPHONE_IPAD=NO",
            ]

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

    @staticmethod
    def _variant_to_platform(variant_name: str) -> Optional[str]:
        """Map an xcframework variant directory name (e.g. ios-arm64-simulator)
        back to the internal platform name used by PLATFORM_BUILD_DIRS."""
        # Mac Catalyst variants are named e.g. `ios-arm64_x86_64-maccatalyst`,
        # so we have to check this suffix BEFORE the `ios-` prefix below.
        if "-maccatalyst" in variant_name:
            return "macCatalyst"

        is_simulator = "simulator" in variant_name
        prefix_map = {
            "macos-": "macOS",
            "ios-": "iOS_Simulator" if is_simulator else "iOS",
            "tvos-": "tvOS_Simulator" if is_simulator else "tvOS",
            "watchos-": "watchOS_Simulator" if is_simulator else "watchOS",
            "xros-": "visionOS_Simulator" if is_simulator else "visionOS",
        }
        for prefix, platform in prefix_map.items():
            if variant_name.startswith(prefix):
                return platform
        return None

    def _copy_swift_modules(self, module: str, xcframework_path: Path):
        """Copy swift modules to xcframework."""
        if not xcframework_path.exists():
            return

        for variant in xcframework_path.iterdir():
            if variant.name == "Info.plist" or not variant.is_dir():
                continue

            platform = self._variant_to_platform(variant.name)
            if platform is None:
                continue

            build_dir, products_subdir = self.PLATFORM_BUILD_DIRS[platform]
            products_dir = self.source / build_dir / "Build" / "Products" / products_subdir

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

        platform_lines = (
            self.LOWERED_PLATFORM_LINES if self.lower_platforms
            else self.UPSTREAM_PLATFORM_LINES
        )
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
            *(f"        {line}," for line in sorted(platform_lines)),
            "    ],",
            "    products: [",
        ]

        # Public products — skip ones not present in the selected tag
        # (e.g. SwiftWarningControl only exists from 603.0.1 onward).
        modules_set = set(self.modules)
        for product in self.PUBLIC_PRODUCTS:
            if product not in modules_set:
                print(f"  Skipping product {product}: not present in tag {self.tag}")
                continue
            lines.append(f'        .library(name: "{product}", targets: ["{product}_Aggregation"]),')

        # Aliased products — public name maps to an internal target name; the
        # underlying target must exist as a built module.
        for public_name, target_name in self.ALIASED_PRODUCTS.items():
            if target_name not in modules_set:
                print(f"  Skipping aliased product {public_name}: target {target_name} not present in tag {self.tag}")
                continue
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
                url = f"{self.base_url}/{self.release_tag}/{module}.xcframework.zip"
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

    def publish_release(self):
        """Upload built xcframework zips to a GitHub Release via `gh`.

        Creates the release at self.release_tag if missing; otherwise uploads
        with --clobber to overwrite existing assets. self.release_tag may
        differ from self.tag (e.g. `<tag>-lower-platforms`) so the same
        upstream version can be published as multiple distinct releases.
        """
        if not self.publish:
            return
        if not self.release_repo:
            raise RuntimeError("publish_release called without release_repo set")

        # Verify gh is installed and authenticated before uploading.
        try:
            subprocess.run(["gh", "auth", "status"], check=True, capture_output=True)
        except FileNotFoundError as exc:
            raise RuntimeError("--publish requires the GitHub CLI (`gh`) to be installed") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("`gh auth status` failed; run `gh auth login` first") from exc

        zips = sorted(self.dest.glob("*.xcframework.zip"))
        if not zips:
            print(f"  Warning: no xcframework zips found in {self.dest}, skipping publish")
            return

        print(f"\nPublishing release {self.release_tag} to {self.release_repo} ({len(zips)} assets)...")

        view = subprocess.run(
            ["gh", "release", "view", self.release_tag, "--repo", self.release_repo],
            capture_output=True
        )
        zip_paths = [str(z) for z in zips]

        if view.returncode == 0:
            print(f"  Release {self.release_tag} exists, uploading assets with --clobber")
            cmd = [
                "gh", "release", "upload", self.release_tag,
                "--repo", self.release_repo, "--clobber",
            ] + zip_paths
        else:
            print(f"  Creating release {self.release_tag}")
            cmd = [
                "gh", "release", "create", self.release_tag,
                "--repo", self.release_repo,
                "--title", self.release_title or self.release_tag,
                "--notes", self.release_notes or "",
            ] + zip_paths

        subprocess.run(cmd, check=True)
        print(f"  ✓ Published {len(zips)} assets to {self.release_repo}@{self.release_tag}")

    def publish_branch(self):
        """Push generated Package.swift + Sources/ to a release branch.

        Clones the target repo into a temporary directory, creates an orphan
        branch (independent history), copies the package files in, then
        force-pushes. Force-push makes re-runs idempotent — repeated builds
        for the same tag overwrite the branch with fresh content.
        """
        if not self.publish_branch_flag:
            return
        if not self.branch_repo:
            raise RuntimeError("publish_branch called without branch_repo set")

        try:
            subprocess.run(["gh", "auth", "status"], check=True, capture_output=True)
        except FileNotFoundError as exc:
            raise RuntimeError("--publish-branch requires the GitHub CLI (`gh`) to be installed") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("`gh auth status` failed; run `gh auth login` first") from exc

        sources_dir = self.output_file.parent / "Sources"
        if not self.output_file.exists():
            raise RuntimeError(f"Package.swift not found at {self.output_file}; run generation first")
        if not sources_dir.exists():
            raise RuntimeError(f"Sources/ not found at {sources_dir}; run generation first")

        print(f"\nPublishing branch {self.branch_name} to {self.branch_repo}...")

        tmp_clone = Path(tempfile.mkdtemp(prefix='swift-syntax-pkg-branch-'))
        try:
            subprocess.run(
                ["gh", "repo", "clone", self.branch_repo, str(tmp_clone)],
                check=True
            )

            # Orphan branch — independent history. Existing remote branch is
            # overwritten by the force-push below.
            subprocess.run(
                ["git", "-C", str(tmp_clone), "checkout", "--orphan", self.branch_name],
                check=True
            )

            for item in tmp_clone.iterdir():
                if item.name == '.git':
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

            shutil.copy2(self.output_file, tmp_clone / "Package.swift")
            shutil.copytree(sources_dir, tmp_clone / "Sources")

            subprocess.run(
                ["git", "-C", str(tmp_clone), "add", "Package.swift", "Sources"],
                check=True
            )
            subprocess.run(
                ["git", "-C", str(tmp_clone), "commit",
                 "-m", f"Release swift-syntax {self.release_tag} binary package"],
                check=True
            )
            # Push to a fully-qualified ref to avoid any tag/branch ambiguity
            # (gh release create earlier may have produced a same-name tag).
            subprocess.run(
                ["git", "-C", str(tmp_clone), "push", "--force", "origin",
                 f"HEAD:refs/heads/{self.branch_name}"],
                check=True
            )
            print(f"  ✓ Pushed branch {self.branch_name} to {self.branch_repo}")
        finally:
            shutil.rmtree(tmp_clone, ignore_errors=True)

    def run(self):
        """Run the complete build process."""
        print(f"Building swift-syntax {self.tag}")
        print(f"Platforms: {', '.join(self.platforms)}")
        print(f"Mode: {self.mode}")
        print(f"Output: {self.output_file}")
        if self.lower_platforms:
            print("Deployment targets: lowered (macOS 10.13 / iOS 12 / tvOS 12 / watchOS 4)")
        else:
            print("Deployment targets: upstream (macOS 10.15 / iOS 13 / tvOS 13 / watchOS 6)")
        if self.release_tag != self.tag:
            print(f"Release tag: {self.release_tag} (build tag: {self.tag})")
        if self.publish:
            print(f"Publish: {self.release_repo}")
        if self.publish_branch_flag:
            print(f"Publish branch: {self.branch_repo} ({self.branch_name})")
        print()

        self.clone_repo()
        self.patch_package_swift()
        if self.lower_platforms:
            self.lower_upstream_platforms()
            self.patch_upstream_source_compat()
        self.extract_modules()
        self.parse_dependencies()
        self.build_all_modules()
        self.compute_checksums()
        self.create_sources_directory()
        self.generate_package_swift()
        self.publish_release()
        self.publish_branch()

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
    %(prog)s --tag 603.0.1 --all-platforms
    %(prog)s --tag 603.0.1 --mode remote --base-url "https://example.com/frameworks"
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
        help=(
            "Platforms to build, comma-separated (default: %(default)s). "
            "Valid: " + ", ".join(SwiftSyntaxBuilder.ALL_PLATFORMS)
        )
    )
    parser.add_argument(
        "--all-platforms",
        action="store_true",
        help="Build for every supported platform (overrides --platforms)"
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
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Upload built xcframework zips to a GitHub Release (requires `gh` CLI)"
    )
    parser.add_argument(
        "--release-repo",
        help="Target repo for --publish in OWNER/REPO form (default: detect from current git origin)"
    )
    parser.add_argument(
        "--release-title",
        help="Release title for --publish (default: tag)"
    )
    parser.add_argument(
        "--release-notes",
        help="Release notes body for --publish (default: empty)"
    )
    parser.add_argument(
        "--publish-branch",
        action="store_true",
        help=(
            "Push generated Package.swift + Sources/ to a release branch on "
            "the target repo (orphan branch, force-pushed; idempotent across re-runs)"
        )
    )
    parser.add_argument(
        "--branch-name",
        help="Branch name for --publish-branch (default: release/<tag>)"
    )
    parser.add_argument(
        "--branch-repo",
        help=(
            "Target repo for --publish-branch in OWNER/REPO form "
            "(default: --release-repo if set, else detected from current git origin)"
        )
    )
    parser.add_argument(
        "--lower-platforms",
        action="store_true",
        help=(
            "Lower upstream swift-syntax deployment targets to macOS 10.13 / "
            "iOS 12 / tvOS 12 / watchOS 4 (macCatalyst stays at 13). Patches "
            "upstream sources to compile under the lower minimums and emits a "
            "matching `platforms` block in the generated Package.swift. "
            "Off by default — upstream macOS 10.15 / iOS 13 / tvOS 13 / "
            "watchOS 6 are kept and no swift-syntax files are touched. When "
            "enabled, the default --release-tag and --branch-name gain a "
            "`-lower-platforms` suffix so the new artefacts don't overwrite "
            "the original release/branch."
        )
    )
    parser.add_argument(
        "--release-tag",
        help=(
            "GitHub Release tag used by --publish and as the default "
            "--branch-name (default: --tag, or `<tag>-lower-platforms` when "
            "--lower-platforms is set). Lets a single upstream version be "
            "published as two distinct releases without overwriting each other."
        )
    )

    args = parser.parse_args()

    # When --publish or --publish-branch is set without an explicit repo, try
    # to detect the target repo from the current directory's git origin.
    needs_detection = (
        (args.publish and not args.release_repo)
        or (args.publish_branch and not args.branch_repo and not args.release_repo)
    )
    detected_repo: Optional[str] = None
    if needs_detection:
        try:
            detected_repo = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
                capture_output=True, text=True, check=True
            ).stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            detected_repo = None

    if args.publish and not args.release_repo:
        if not detected_repo:
            parser.error(
                "--publish needs --release-repo OWNER/REPO "
                "(could not detect via `gh repo view` from current directory)"
            )
        args.release_repo = detected_repo

    if args.publish_branch and not args.branch_repo:
        args.branch_repo = args.release_repo or detected_repo
        if not args.branch_repo:
            parser.error(
                "--publish-branch needs --branch-repo OWNER/REPO "
                "(could not detect via `gh repo view` from current directory)"
            )

    # When publishing in remote mode without an explicit base URL, default to
    # the GitHub Release asset URL so generated Package.swift points at the
    # uploaded assets.
    if args.publish and args.mode == "remote" and not args.base_url:
        args.base_url = f"https://github.com/{args.release_repo}/releases/download"

    # Validate arguments
    if args.mode == "remote" and not args.base_url:
        parser.error("--base-url is required when --mode is 'remote'")

    # Parse platforms
    if args.all_platforms:
        platforms = list(SwiftSyntaxBuilder.ALL_PLATFORMS)
    else:
        platforms = [p.strip() for p in args.platforms.split(",")]
        unknown = [p for p in platforms if p not in SwiftSyntaxBuilder.PLATFORM_BUILD_DIRS]
        if unknown:
            parser.error(
                f"Unknown platform(s): {unknown}. "
                f"Valid: {sorted(SwiftSyntaxBuilder.PLATFORM_BUILD_DIRS)}"
            )

    # Create builder and run
    builder = SwiftSyntaxBuilder(
        repo=args.repo,
        tag=args.tag,
        platforms=platforms,
        mode=args.mode,
        base_url=args.base_url,
        output_file=args.output,
        parallel_builds=args.parallel,
        publish=args.publish,
        release_repo=args.release_repo,
        release_title=args.release_title,
        release_notes=args.release_notes,
        publish_branch=args.publish_branch,
        branch_name=args.branch_name,
        branch_repo=args.branch_repo,
        lower_platforms=args.lower_platforms,
        release_tag=args.release_tag,
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
