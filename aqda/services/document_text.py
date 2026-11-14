"""The codeable text of a document: what codings point into."""

import hashlib


# Return the text codings refer to: the transcript of an audio document, else its content.
def codeable_text(document) -> str:
    if document["source_type"] == "audio":
        return document["transcript"] or ""
    return document["content"] or ""


# Return a fingerprint of a text (sha256 of its UTF-8 bytes, as hex).
def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
