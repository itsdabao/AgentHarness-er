"""Read GGUF metadata without loading weights or installing model libraries."""

import json
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

FORMATS = {
    0: "B",
    1: "b",
    2: "H",
    3: "h",
    4: "I",
    5: "i",
    6: "f",
    7: "?",
    10: "Q",
    11: "q",
    12: "d",
}


def number(stream: BinaryIO, fmt: str) -> Any:
    return struct.unpack("<" + fmt, stream.read(struct.calcsize("<" + fmt)))[0]


def value(stream: BinaryIO, kind: int, retain: bool = True) -> Any:
    if kind == 8:
        length = number(stream, "Q")
        if retain:
            return stream.read(length).decode("utf-8")
        stream.seek(length, 1)
        return None
    if kind == 9:
        subtype, count = number(stream, "I"), number(stream, "Q")
        items = []
        for _ in range(count):
            item = value(stream, subtype, retain)
            if retain:
                items.append(item)
        return items if retain else None
    return number(stream, FORMATS[kind])


def inspect(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        if stream.read(4) != b"GGUF":
            raise ValueError("Not GGUF")
        result: dict[str, Any] = {
            "file": str(path),
            "bytes": path.stat().st_size,
            "version": number(stream, "I"),
            "tensor_count": number(stream, "Q"),
        }
        count = number(stream, "Q")
        metadata = {}
        for _ in range(count):
            key = value(stream, 8)
            kind = number(stream, "I")
            keep = (
                key.startswith("general.")
                or key.endswith(
                    (
                        ".block_count",
                        ".context_length",
                        ".embedding_length",
                    )
                )
                or key == "tokenizer.ggml.model"
            )
            item = value(stream, kind, keep)
            if keep:
                metadata[key] = item
        result["metadata"] = metadata
        return result


if __name__ == "__main__":
    print(json.dumps([inspect(Path(p)) for p in sys.argv[1:]], indent=2, ensure_ascii=False))
