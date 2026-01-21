"""
Argos Translate Model Installer

Purpose
- Download and install Argos Translate language packages for a predefined set of
  translation directions.
- Intended as a one-time (or occasional) setup step so the runtime translator can
  run fully offline using the Argos backend.

Operational notes
- This script requires internet access to:
  1) update the Argos package index (metadata)
  2) download the selected language packages
- After installation, Argos can translate offline (no internet required at runtime).

How it works
1) Update the package index (fetches available packages list)
2) For each desired language direction (src -> tgt):
   - find a matching package in the index
   - download it
   - install it into the local Argos package store
3) Print progress and exit

Customizing
- Extend/modify PAIRS to add more language directions as needed.
- If multiple packages match a direction, this script picks the first match.
  In production, you might want to select by version, quality, or model type.
"""

import argostranslate.package


# -----------------------------------------------------------------------------
# Configuration: translation directions to install
# -----------------------------------------------------------------------------
# Each tuple is (source_language_code, target_language_code).
# Codes follow ISO language code conventions used by Argos (e.g., "en", "de").
PAIRS = [
    ("en", "de"),
    ("de", "en"),
]


def main() -> None:
    """
    Install Argos language packages for all directions in PAIRS.

    This function:
    - updates the online package index once
    - iterates through PAIRS and installs matching packages
    - prints a simple progress log for each step
    """
    print("Updating Argos package index (needs internet once)...")
    argostranslate.package.update_package_index()

    # Fetch all packages available for download after the index update.
    available = argostranslate.package.get_available_packages()

    # Kept for potential diagnostics or future filtering (not used in current logic).
    wanted = {(a, b) for a, b in PAIRS}
    _ = wanted

    for src, tgt in PAIRS:
        # Find packages that match the requested direction exactly.
        matches = [p for p in available if p.from_code == src and p.to_code == tgt]
        if not matches:
            print(f"Not found: {src}->{tgt}")
            continue

        # If multiple packages exist, select the first one.
        pkg = matches[0]

        print(f"Downloading: {src}->{tgt} ...")
        path = pkg.download()

        print(f"Installing: {src}->{tgt} ...")
        argostranslate.package.install_from_path(path)

        print(f"Installed: {src}->{tgt}")

    print("Done.")


if __name__ == "__main__":
    main()
