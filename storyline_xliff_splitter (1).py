#!/usr/bin/env python3
"""
Articulate Storyline XLIFF 1.2 splitter — CLI + Streamlit UI

This single file can be used TWO ways:
  A) **Command line** (batch/scripting)
  B) **Streamlit web app** (no-code UI)

What it does
------------
Given a Storyline-exported XLIFF 1.2 file, it produces two new XLIFFs:
  1) *Notes only*:       contains ONLY the translation units associated with course notes
  2) *Main (no notes/ALT)*: contains ALL translatable units EXCEPT those associated with course notes or ALT text

Quick start
-----------
CLI:
    python storyline_xliff_splitter.py input.xlf \
      --out-notes notes_only.xlf \
      --out-main content_no_notes_alt.xlf

Streamlit UI:
    pip install streamlit
    streamlit run storyline_xliff_splitter.py

Then open the local URL it prints (usually http://localhost:8501).

— Tested with Python 3.9+
"""

from __future__ import annotations
import argparse
import copy
import io
import os
import re
import sys
import xml.etree.ElementTree as ET
from typing import Iterable, List, Optional, Tuple

# Try enabling Streamlit mode if available
_ST_MODE = False
try:
    import streamlit as st  # type: ignore
    _ST_MODE = True
except Exception:
    _ST_MODE = False

# XLIFF 1.2 default namespace (commonly present but sometimes omitted in Storyline exports)
XLIFF_NS = "urn:oasis:names:tc:xliff:document:1.2"

# Tag helpers: handle namespaced and non-namespaced documents gracefully

def q(tag: str) -> str:
    return f"{{{XLIFF_NS}}}{tag}"


def findall(elem: ET.Element, tag: str) -> List[ET.Element]:
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
    texts: List[str] = []
    for n in findall(tu, "note"):
        t = get_text_recursive(n)
        if t:
            texts.append(t)
    # <context-group>/<context> sometimes present
    for cg in tu.findall("context-group") + tu.findall(q("context-group")):
        for c in cg.findall("context") + cg.findall(q("context")):
            t = get_text_recursive(c)
            if t:
                texts.append(t)
            for v in c.attrib.values():
                if v:
                    texts.append(v)
    for key in ("id", "resname", "res-id", "extradata", "x-articulate-part"):
        val = tu.attrib.get(key)
        if val:
            texts.append(val)
    return [t for t in (s.strip() for s in texts) if t]


def matches_any(texts: Iterable[str], patterns: List[re.Pattern]) -> bool:
    for t in texts:
        for p in patterns:
            if p.search(t):
                return True
    return False


def build_patterns(expr: str) -> List[re.Pattern]:
    parts = re.split(r"(?<!\\)\|", expr) if "|" in expr else [expr]
    compiled: List[re.Pattern] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        compiled.append(re.compile(part, flags=re.IGNORECASE))
    return compiled


def clone_shell(tree: ET.ElementTree) -> ET.ElementTree:
    root = tree.getroot()
    new_root = copy.deepcopy(root)
    # Clear all trans-units in the copy
    for f in findall(new_root, "file"):
        body = find(f, "body")
        if body is not None:
            to_remove = [tu for tu in list(body) if tu.tag in {q("trans-unit"), "trans-unit"}]
            for tu in to_remove:
                body.remove(tu)
    return ET.ElementTree(new_root)


def append_tu_to_output_shell(shell: ET.ElementTree, tu: ET.Element):
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
) -> Tuple[ET.ElementTree, ET.ElementTree]:
    notes_only = clone_shell(tree)
    main_no_notes_alt = clone_shell(tree)
    for tu in iter_trans_units(tree.getroot()):
        texts = contexts_for_tu(tu)
        is_notes = matches_any(texts, notes_patterns)
        is_alt = matches_any(texts, alt_patterns)
        if is_notes:
            append_tu_to_output_shell(notes_only, tu)
            continue
        if is_alt:
            continue
        append_tu_to_output_shell(main_no_notes_alt, tu)
    return notes_only, main_no_notes_alt


def write_tree(tree: ET.ElementTree, path: str):
    if tree.getroot().tag.startswith("{") and XLIFF_NS in tree.getroot().tag:
        ET.register_namespace("", XLIFF_NS)
    tree.write(path, encoding="utf-8", xml_declaration=True)

############################
# CLI mode
############################

def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Split Storyline XLIFF into notes-only and main-without-notes-or-alt.")
    parser.add_argument("input", nargs="?", help="Path to input XLIFF 1.2 file exported from Articulate Storyline")
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


def _main_cli(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    if not args.input:
        print("Error: missing input XLIFF path.\nTry: python storyline_xliff_splitter.py input.xlf", file=sys.stderr)
        return 2
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

############################
# Streamlit UI mode
############################

def _bytes_from_tree(tree: ET.ElementTree) -> bytes:
    buf = io.BytesIO()
    if tree.getroot().tag.startswith("{") and XLIFF_NS in tree.getroot().tag:
        ET.register_namespace("", XLIFF_NS)
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


def _main_streamlit() -> None:
    st.set_page_config(page_title="Storyline XLIFF Splitter", layout="centered")
    st.title("Storyline XLIFF Splitter")
    st.caption("Create two XLIFFs: Notes only, and Main without Notes & ALT text.")

    with st.expander("Patterns (advanced)"):
        notes_expr = st.text_input(
            "Notes patterns (regex |-separated)",
            value=r"Notes|Slide Notes|Player Notes|note(s)?|x-?notes|storyline notes",
        )
        alt_expr = st.text_input(
            "ALT text patterns (regex |-separated)",
            value=r"Alt ?Text|AltText|Alternate Text|Accessibility|A11y|Image description|Alt of",
        )

    uploaded = st.file_uploader("Upload Storyline XLIFF 1.2", type=["xlf", "xliff", "xml"]) 
    run = st.button("Split XLIFF", type="primary", disabled=uploaded is None)

    if run and uploaded is not None:
        data = uploaded.read()
        try:
            tree = ET.parse(io.BytesIO(data))
        except ET.ParseError as e:
            st.error(f"Failed to parse XLIFF: {e}")
            return
        notes_patterns = build_patterns(notes_expr)
        alt_patterns = build_patterns(alt_expr)
        notes_only, main_no_notes_alt = filter_xliff(tree, notes_patterns, alt_patterns)
        st.success("Split complete.")
        st.download_button(
            label="Download — notes_only.xlf",
            data=_bytes_from_tree(notes_only),
            file_name="notes_only.xlf",
            mime="application/xml",
        )
        st.download_button(
            label="Download — content_no_notes_alt.xlf",
            data=_bytes_from_tree(main_no_notes_alt),
            file_name="content_no_notes_alt.xlf",
            mime="application/xml",
        )
        st.markdown("\n")
        with st.expander("Preview (first 1000 chars)"):
            st.code(_bytes_from_tree(notes_only)[:1000].decode("utf-8", errors="ignore"))
            st.code(_bytes_from_tree(main_no_notes_alt)[:1000].decode("utf-8", errors="ignore"))


if __name__ == "__main__":
    if _ST_MODE and os.environ.get("STREAMLIT_SERVER_ENABLED", "0") == "1":
        # When run by `streamlit run`, this environment variable is set.
        _main_streamlit()
    elif _ST_MODE and any("streamlit" in s.lower() for s in sys.argv):
        # Fallback: if someone does `python storyline_xliff_splitter.py streamlit`
        _main_streamlit()
    else:
        raise SystemExit(_main_cli())
