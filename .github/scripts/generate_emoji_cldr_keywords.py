"""Generate a compact emoji keyword module from Unicode CLDR annotations.

The picker keeps the displayed Emoji 17.0 rows in ``emoji_picker.py``.  CLDR
omits U+FE0F presentation selectors from annotation keys, so this generator
maps each annotation back to the exact spelling used by those rows before it
writes the module.
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from pathlib import Path
import unicodedata
import xml.etree.ElementTree as ET


def _search_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    return "".join(
        character for character in decomposed
        if not unicodedata.combining(character)
    ).casefold()


def _emoji_categories(source_path: Path) -> tuple:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "EMOJI_CATEGORIES"
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"EMOJI_CATEGORIES not found in {source_path}")


def _unique_words(annotation: str) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()
    for word in _search_text(annotation.replace("|", " ")).split():
        if word not in seen:
            seen.add(word)
            words.append(word)
    return words


def generate(
    picker_path: Path,
    annotation_paths: list[Path],
    output_path: Path,
    variable_name: str,
    locale: str,
    cldr_release: str,
) -> None:
    ordered_emojis: list[str] = []
    exact_by_cldr_key: dict[str, list[str]] = defaultdict(list)
    seen_emojis: set[str] = set()
    for _category, values in _emoji_categories(picker_path):
        for emoji in values.split():
            if emoji in seen_emojis:
                continue
            seen_emojis.add(emoji)
            ordered_emojis.append(emoji)
            exact_by_cldr_key[emoji.replace("\ufe0f", "")].append(emoji)

    words_by_emoji: dict[str, list[str]] = defaultdict(list)
    seen_words: dict[str, set[str]] = defaultdict(set)
    for annotation_path in annotation_paths:
        root = ET.parse(annotation_path).getroot()
        for annotation in root.iter("annotation"):
            cldr_key = annotation.attrib.get("cp", "").replace("\ufe0f", "")
            matching_emojis = exact_by_cldr_key.get(cldr_key, ())
            if not matching_emojis:
                continue
            words = _unique_words("".join(annotation.itertext()))
            for emoji in matching_emojis:
                for word in words:
                    if word not in seen_words[emoji]:
                        seen_words[emoji].add(word)
                        words_by_emoji[emoji].append(word)

    lines = [
        '"""Generated Unicode CLDR emoji search terms; do not edit by hand.',
        "",
        f"Locale: {locale}",
        f"Source release: unicode-org/cldr {cldr_release}",
        '"""',
        "",
        f"{variable_name} = {{",
    ]
    for emoji in ordered_emojis:
        words = words_by_emoji.get(emoji)
        if not words:
            continue
        lines.append(f"    {emoji!r}: {' '.join(words)!r},")
    lines.extend(("}", ""))
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("picker", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("annotations", nargs="+", type=Path)
    parser.add_argument("--variable", required=True)
    parser.add_argument("--locale", required=True)
    parser.add_argument("--cldr-release", required=True)
    args = parser.parse_args()
    generate(
        args.picker,
        args.annotations,
        args.output,
        args.variable,
        args.locale,
        args.cldr_release,
    )


if __name__ == "__main__":
    main()
