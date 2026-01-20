import argostranslate.package

PAIRS = [
    ("en", "de"),
    ("de", "en"),
]

def main():
    print("Updating Argos package index (needs internet once)...")
    argostranslate.package.update_package_index()

    available = argostranslate.package.get_available_packages()
    wanted = {(a, b) for a, b in PAIRS}

    for src, tgt in PAIRS:
        matches = [p for p in available if p.from_code == src and p.to_code == tgt]
        if not matches:
            print(f"Not found: {src}->{tgt}")
            continue

        pkg = matches[0]
        print(f"Downloading: {src}->{tgt} ...")
        path = pkg.download()
        print(f"Installing: {src}->{tgt} ...")
        argostranslate.package.install_from_path(path)
        print(f"Installed: {src}->{tgt}")

    print("Done.")

if __name__ == "__main__":
    main()
