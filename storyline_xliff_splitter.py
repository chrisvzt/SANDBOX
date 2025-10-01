#!/usr/bin/env python3
"""
Articulate Storyline XLIFF 1.2 splitter

Given a Storyline-exported XLIFF 1.2 file, produce two new XLIFFs:
  1) notes_only.xlf:      contains ONLY the translation units associated with course notes
  2) content_no_notes_alt.xlf: contains ALL translatable units EXCEPT those associated with course notes or ALT text

Heuristics are tuned for common Storyline XLIFF conventions but can be customized with CLI flags.

Usage:
    python storyline_xliff_splitter.py input.xlf \
        --out-notes notes_only.xlf \
        --out-main content_no_notes_alt.xlf \
        [--notes-patterns "Notes|Slide Notes|Player Notes|note(s)?" \
         --alt-patterns "Alt ?Text|AltText|Alternate Text|Accessibility|A11y|Image description"]

Notes:
- This script preserves structure (file/header/body) and metadata.
- It only filters <trans-unit> elements. Non-translatable units are left untouched unless they’re <trans-unit>.
- It will copy over <seg-source>, <source>, <target>, <note>, and <context-group> content as-is.

Tested with Python 3.9+.
"""

from __future__ import annotations
import argparse
import copy
import re
import sys
import xml.etree.ElementTree as ET
from typing import Iterable, List, Optional

# XLIFF 1.2 default namespace (commonly present but sometimes omitted in Storyline exports)
XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"
NSMAP = {"x": XLIFF_NS}

# Tag helpers: handle namespaced and non-namespaced documents gracefully

def q(tag: str) -> str:
    """Qualify a tag name with the xliff namespace."""
    return f"{{{XLIFF_NS}}}{tag}"


def findall(elem: ET.Element, tag: str) -> List[ET.Element]:
    # Try namespaced first; if nothing found, fallback to non-namespaced tag
    results = elem.findall(q(tag))
    if not results:
        results = elem.findall(tag)
    return results


def find(elem: ET.Element, tag: str) -> Optional[ET.Element]:
    node = elem.find(q(tag))
    if node is None:
        node = elem.find(tag)
    return node


def iter_trans_units(root: ET.Element) -> Iterable[ET.Element]:
    # XLIFF 1.2 structure: <xliff><file><body><trans-unit/></body></file></xliff>
    for f in findall(root, "file"):
        body = find(f, "body")
        if body is None:
            continue
        for tu in findall(body, "trans-unit"):
            yield tu


def get_text_recursive(elem: Optional[ET.Element]) -> str:
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def contexts_for_tu(tu: ET.Element) -> List[str]:
    """Collect textual hints for a trans-unit from common Storyline locations."""
    texts: List[str] = []

    # <note> elements often carry hints like "Notes", "Alt Text" etc.
    for n in findall(tu, "note"):
        t = get_text_recursive(n)
        if t:
            texts.append(t)

    # <context-group>/<context> (not standardized, but used by many tools)
    for cg in tu.findall("context-group") + tu.findall(q("context-group")):
        for c in cg.findall("context") + cg.findall(q("context")):
            t = get_text_recursive(c)
            if t:
                texts.append(t)
            # Also consider attributes like context-type
            for v in c.attrib.values():
                if v:
                    texts.append(v)

    # Attributes can be indicative: id, resname, extradata, etc.
    for key in ("id", "resname", "res-id", "extradata", "x-articulate-part"):
        val = tu.attrib.get(key)
        if val:
            texts.append(val)

    # Sometimes hints are on the parent <group> or wrapper <g>
    parent = tu.getparent() if hasattr(tu, "getparent") else None  # lxml only; ElementTree lacks getparent
    # ElementTree doesn’t provide parent access; skipped unless running with lxml

    # Normalize
    return [t for t in (s.strip() for s in texts) if t]


def matches_any(texts: Iterable[str], patterns: List[re.Pattern]) -> bool:
    for t in texts:
        for p in patterns:
            if p.search(t):
                return True
    return False


def build_patterns(expr: str) -> List[re.Pattern]:
    # Accept a single regex containing alternatives (e.g., "Alt ?Text|AltText|Alternate Text")
    # Split on unescaped | for readability
    parts = re.split(r"(?<!\\)\|", expr) if "|" in expr else [expr]
    compiled: List[re.Pattern] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        compiled.append(re.compile(part, flags=re.IGNORECASE))
    return compiled


def clone_shell(tree: ET.ElementTree) -> ET.ElementTree:
    """Create an empty copy of the XLIFF document with <file>/<body> shells preserved."""
    root = tree.getroot()
    new_root = copy.deepcopy(root)

    # Clear all trans-units in the copy
    for f in findall(new_root, "file"):
        body = find(f, "body")
        if body is not None:
            # Remove only direct children that are trans-units
            to_remove = [tu for tu in list(body) if tu.tag in {q("trans-unit"), "trans-unit"}]
            for tu in to_remove:
                body.remove(tu)

    return ET.ElementTree(new_root)


def append_tu_to_output_shell(shell: ET.ElementTree, tu: ET.Element):
    # Append <trans-unit> to the first <file>/<body> matching the original structure.
    # If multiple <file> elements exist, we naively put all TUs into the first one with a <body>.
    root = shell.getroot()
    for f in findall(root, "file"):
        body = find(f, "body")
        if body is not None:
            body.append(copy.deepcopy(tu))
            return
    raise RuntimeError("No <file>/<body> found in shell to append trans-unit.")


def filter_xliff(
    tree: ET.ElementTree,
    notes_patterns: List[re.Pattern],
    alt_patterns: List[re.Pattern],
):
    notes_only = clone_shell(tree)
    main_no_notes_alt = clone_shell(tree)

    for tu in iter_trans_units(tree.getroot()):
        texts = contexts_for_tu(tu)
        is_notes = matches_any(texts, notes_patterns)
        is_alt = matches_any(texts, alt_patterns)

        # Add to notes-only if notes
        if is_notes:
            append_tu_to_output_shell(notes_only, tu)
            # Do NOT include in main
            continue

        # Exclude ALT text from main
        if is_alt:
            continue

        # Otherwise include in main
        append_tu_to_output_shell(main_no_notes_alt, tu)

    return notes_only, main_no_notes_alt


def write_tree(tree: ET.ElementTree, path: str):
    # Ensure namespace prefix for pretty outputs
    root = tree.getroot()
    # Register namespace only if root is namespaced; avoids duplicating ns decls
    if root.tag.startswith("{") and XLIFF_NS in root.tag:
        ET.register_namespace("", XLIFF_NS)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split Storyline XLIFF into notes-only and main-without-notes-or-alt.")
    parser.add_argument("input", help="Path to input XLIFF 1.2 file exported from Articulate Storyline")
    parser.add_argument("--out-notes", default="notes_only.xlf", help="Output path for notes-only XLIFF (default: notes_only.xlf)")
    parser.add_argument("--out-main", default="content_no_notes_alt.xlf", help="Output path for main XLIFF without notes & alt text (default: content_no_notes_alt.xlf)")
    parser.add_argument(
        "--notes-patterns",
        default=r"Notes|Slide Notes|Player Notes|note(s)?|x-?notes|storyline notes",
        help="Regex (alternations allowed) to detect course notes in <note>, <context>, id/resname, etc.",
    )
    parser.add_argument(
        "--alt-patterns",
        default=r"Alt ?Text|AltText|Alternate Text|Accessibility|A11y|Image description|Alt of",
        help="Regex (alternations allowed) to detect ALT text entries.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        tree = ET.parse(args.input)
    except ET.ParseError as e:
        print(f"Failed to parse XLIFF: {e}", file=sys.stderr)
        return 2

    notes_patterns = build_patterns(args.notes_patterns)
    alt_patterns = build_patterns(args.alt_patterns)

    notes_only, main_no_notes_alt = filter_xliff(tree, notes_patterns, alt_patterns)

    write_tree(notes_only, args.out_notes)
    write_tree(main_no_notes_alt, args.out_main)

    print(f"✓ Wrote {args.out_notes} (notes only)")
    print(f"✓ Wrote {args.out_main} (everything except notes & ALT text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
