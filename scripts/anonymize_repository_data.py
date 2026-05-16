"""
Anonymize repository data.

This script replaces professor identifiers such as RFC-like codes with
generic anonymous IDs (P000001, P000002, ...).

It processes:
- data/samples/*.json
- outputs/*.csv

The generated mapping is stored locally as:
- scripts/anonymization_map_LOCAL.csv

Do NOT publish the mapping file.
"""

from pathlib import Path
import re
import csv

ROOT = Path(__file__).resolve().parents[1]

TARGET_PATTERNS = [
    ROOT / "data" / "samples" / "*.json",
    ROOT / "outputs" / "*.csv",
]

# Mexican RFC-like pattern used for professor identifiers:
# 3 or 4 uppercase letters + 6 digits + 3 alphanumeric characters.
RFC_PATTERN = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b")

mapping = {}


def get_anonymous_id(original: str) -> str:
    if original not in mapping:
        mapping[original] = f"P{len(mapping) + 1:06d}"
    return mapping[original]


def anonymize_text(text: str) -> str:
    return RFC_PATTERN.sub(lambda match: get_anonymous_id(match.group(0)), text)


def main() -> None:
    files = []

    for pattern in TARGET_PATTERNS:
        files.extend(ROOT.glob(str(pattern.relative_to(ROOT))))

    files = sorted(set(files))

    if not files:
        print("No files found to anonymize.")
        return

    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        anonymized = anonymize_text(text)
        path.write_text(anonymized, encoding="utf-8")
        print(f"Anonymized: {path.relative_to(ROOT)}")

    map_path = ROOT / "scripts" / "anonymization_map_LOCAL.csv"
    with map_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["original_identifier", "anonymous_identifier"])
        for original, anonymous in sorted(mapping.items(), key=lambda item: item[1]):
            writer.writerow([original, anonymous])

    print()
    print(f"Total identifiers anonymized: {len(mapping)}")
    print(f"Local mapping written to: {map_path.relative_to(ROOT)}")
    print("IMPORTANT: Do not commit or publish the mapping file.")


if __name__ == "__main__":
    main()