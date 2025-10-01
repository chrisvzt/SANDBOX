#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Storyline Localization Estimator (Streamlit)
-------------------------------------------
Upload an Articulate Storyline *.story file to extract likely translatable strings
and produce a quick localization estimate: total words, segments, per-file breakdown,
and counts of media assets.

Run:
  pip install streamlit
  streamlit run storyline_loc_estimator.py
"""

import io
import json
import re
import zipfile
from html import unescape
from xml.etree import ElementTree as ET

import streamlit as st

WS_RE = re.compile(r'\s+', re.UNICODE)

TRANSLATABLE_ATTRS = {
    'alt','title','label','aria-label','aria_title','tooltip','placeholder',
    'value','caption','text','content','name','displayName'
}

NOISE_ATTRS = {
    'id','uid','guid','rid','x','y','w','h','left','top','width','height',
    'style','class','ctype','type','lang','language','src','href','fill','stroke',
    'font','fontSize','color','alignment','bold','italic','underline'
}

LIKELY_TEXT_TAGS = {
    'text','p','span','div','tspan','title','desc','caption','para','run',
    'li','h1','h2','h3','h4','h5','h6','td','th','label','value','name','g'
}

SKIP_TAGS = {
    'style','script','defs','metadata','font','image','img','svg','use','clipPath'
}

def normalize_text(s: str) -> str:
    if s is None:
        return ''
    s = unescape(s)
    s = s.replace('\u00A0', ' ')
    s = WS_RE.sub(' ', s).strip()
    return s

def is_likely_human_text(s: str) -> bool:
    if not s:
        return False
    return bool(re.search(r'[A-Za-zÀ-ÖØ-öø-ÿĀ-žЀ-џא-ת؀-ۿऀ-ॿก-๛一-龥ぁ-ゟ가-힣]', s))

def should_skip_text(s: str) -> bool:
    if not s or s == '-':
        return True
    if re.fullmatch(r'[\W_]+', s):
        return True
    if re.fullmatch(r'[0-9A-Fa-f-]{8,}', s):
        return True
    return False

def iter_zip_entries(zf: zipfile.ZipFile):
    for info in zf.infolist():
        name_lower = info.filename.lower()
        if any(name_lower.endswith(ext) for ext in ('.xml', '.htm', '.html', '.json')) and not info.is_dir():
            yield info

def parse_xml_bytes(data: bytes):
    try:
        root = ET.fromstring(data)
        return root
    except ET.ParseError:
        cleaned = re.sub(rb'[^\x09\x0A\x0D\x20-\xFF]+', b'', data)
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError:
            return None

def walk_xml_collect(root, file_id, out_rows, include_alttext=True):
    stack = [(root, [])]
    while stack:
        elem, path = stack.pop()
        tag = elem.tag.split('}', 1)[-1] if '}' in elem.tag else elem.tag
        if tag in SKIP_TAGS:
            continue

        attrs = { (k.split('}',1)[-1] if '}' in k else k): v for k, v in elem.attrib.items() }
        if not include_alttext:
            for v in list(attrs.values()):
                if isinstance(v, str) and '.AltText' in v:
                    # skip collecting from this node (but still descend in case children hold non-alt text)
                    break

        txt = normalize_text(elem.text or '')
        if txt and (tag in LIKELY_TEXT_TAGS or is_likely_human_text(txt)) and not should_skip_text(txt):
            out_rows.append((file_id, '/'+ '/'.join(path+[tag, '#text']), txt))

        for kk, v in attrs.items():
            if kk in NOISE_ATTRS:
                continue
            val = normalize_text(v)
            if kk in TRANSLATABLE_ATTRS or is_likely_human_text(val):
                if not should_skip_text(val):
                    out_rows.append((file_id, '/'+ '/'.join(path+[tag, f'@{kk}']), val))

        tail = normalize_text(elem.tail or '')
        if tail and is_likely_human_text(tail) and not should_skip_text(tail):
            out_rows.append((file_id, '/'+ '/'.join(path+[tag, '#tail']), tail))

        for child in reversed(list(elem)):
            stack.append((child, path+[tag]))

def extract_from_json(doc, file_id, out_rows):
    if isinstance(doc, dict):
        for k, v in doc.items():
            new_path = f"$.{k}"
            if isinstance(v, (dict, list)):
                extract_from_json(v, file_id, out_rows)
            else:
                if isinstance(v, (str, int, float)):
                    val = normalize_text(str(v))
                    if val and is_likely_human_text(val) and not should_skip_text(val):
                        if k.lower() not in NOISE_ATTRS:
                            out_rows.append((file_id, new_path, val))
    elif isinstance(doc, list):
        for i, v in enumerate(doc):
            extract_from_json(v, file_id, out_rows)

def extract_rows_from_story(story_bytes: bytes, skip_alttext=True):
    rows = []
    media_counts = {"images":0, "audio":0, "video":0, "other":0}
    with zipfile.ZipFile(io.BytesIO(story_bytes), 'r') as zf:
        for info in zf.infolist():
            name_lower = info.filename.lower()
            if name_lower.endswith(('.png','.jpg','.jpeg','.gif','.svg','.webp')):
                media_counts["images"] += 1
            elif name_lower.endswith(('.mp3','.wav','.m4a','.ogg')):
                media_counts["audio"] += 1
            elif name_lower.endswith(('.mp4','.webm','.mov','.m4v')):
                media_counts["video"] += 1
            elif not info.is_dir() and not name_lower.endswith(('.xml','.htm','.html','.json')):
                media_counts["other"] += 1

            if any(name_lower.endswith(ext) for ext in ('.xml', '.htm', '.html', '.json')) and not info.is_dir():
                try:
                    data = zf.read(info)
                except Exception:
                    continue
                if name_lower.endswith('.json'):
                    try:
                        doc = json.loads(data.decode('utf-8', errors='ignore'))
                        extract_from_json(doc, info.filename, rows)
                    except Exception:
                        pass
                    continue
                root = parse_xml_bytes(data)
                if root is not None:
                    walk_xml_collect(root, info.filename, rows, include_alttext=(not skip_alttext))

    seen = set()
    out = []
    for file_id, path, text in rows:
        key = (file_id, path, text)
        if key in seen:
            continue
        seen.add(key)
        out.append((file_id, path, text))
    out.sort(key=lambda r: (r[0].lower(), r[1].lower()))
    return out, media_counts

def word_count(text: str) -> int:
    if not text:
        return 0
    cjk = re.findall(r'[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]', text)
    no_cjk = re.sub(r'[\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF]', ' ', text)
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9']+", no_cjk)
    return len(words) + len(cjk)

import pandas as pd

st.set_page_config(page_title="Storyline Localization Estimator", page_icon="📝", layout="wide")
st.title("📝 Storyline Localization Estimator")
st.caption("Upload an Articulate Storyline *.story file to extract translatable text and estimate localization scope.")

col1, col2 = st.columns([2,1])
with col1:
    uploaded = st.file_uploader("Choose a .story file", type=["story"])
with col2:
    skip_alt = st.checkbox("Skip AltText-like entries", value=True)
    minlen = st.number_input("Min text length", min_value=1, max_value=50, value=1, step=1)

if uploaded:
    with st.spinner("Parsing and analyzing…"):
        rows, media = extract_rows_from_story(uploaded.read(), skip_alttext=skip_alt)
        rows = [r for r in rows if len(r[2]) >= minlen]
        total_segments = len(rows)
        total_words = sum(word_count(t) for _,_,t in rows)

    st.success(f"Extracted {total_segments} strings | ~{total_words} words")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Segments", f"{total_segments}")
    m2.metric("Words (approx.)", f"{total_words}")
    m3.metric("Media files (img/audio/video)", f"{media['images']}/{media['audio']}/{media['video']}")
    m4.metric("Other package files", f"{media['other']}")

    df = pd.DataFrame(rows, columns=["source_file","location","text"])
    df["words"] = df["text"].apply(word_count)
    by_file = df.groupby("source_file")["words"].agg(["count","sum"]).reset_index().rename(columns={"count":"segments","sum":"words"})
    st.subheader("Per-file breakdown")
    st.dataframe(by_file, use_container_width=True)

    st.subheader("Extracted strings")
    st.dataframe(df, use_container_width=True)

    @st.cache_data
    def get_csv_bytes(_df: pd.DataFrame) -> bytes:
        return _df.to_csv(index=False).encode("utf-8")

    @st.cache_data
    def get_json_bytes(_df: pd.DataFrame) -> bytes:
        return _df.to_json(orient="records", force_ascii=False, indent=2).encode("utf-8")

    st.download_button("Download strings (CSV)", data=get_csv_bytes(df), file_name="storyline_strings.csv", mime="text/csv")
    st.download_button("Download strings (JSON)", data=get_json_bytes(df), file_name="storyline_strings.json", mime="application/json")
    st.download_button("Download per-file summary (CSV)", data=get_csv_bytes(by_file), file_name="storyline_summary.csv", mime="text/csv")
else:
    st.info("Upload a *.story file to begin. This tool runs locally in your browser via Streamlit; your file is not uploaded to any server.")
