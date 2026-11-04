#!/usr/bin/env python3
"""Self-check for Screen AI's output handling. No deps (the library itself is not loaded) --
run: python3 test_screenai.py"""

import struct

from screenai import Box, Line, bubbles, line_text, parse_visual_annotation


def varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        out.append(b | (0x80 if v else 0))
        if not v:
            return bytes(out)


def field(number: int, wire: int, payload) -> bytes:
    key = varint(number << 3 | wire)
    if wire == 0:
        return key + varint(payload)
    if wire == 5:
        return key + payload
    return key + varint(len(payload)) + payload


def rect(x, y, w, h, angle=None) -> bytes:
    out = field(1, 0, x) + field(2, 0, y) + field(3, 0, w) + field(4, 0, h)
    return out + (field(5, 5, struct.pack("<f", angle)) if angle is not None else b"")


def line_proto(text, box, block, direction, symbols=()) -> bytes:
    word = b"".join(field(1, 2, field(1, 2, rect(*b)) + field(2, 2, t.encode())) for t, b in symbols)
    return (field(1, 2, word) + field(2, 2, rect(*box)) + field(3, 2, text.encode()) + field(5, 0, block)
            + field(7, 0, direction) + field(10, 5, struct.pack("<f", 0.9)))


def vline(text, x, y, w, h, block=0):
    return Line(text, Box(x, y, w, h), block_id=block, vertical=True)


def test_parses_lines_symbols_and_direction():
    data = field(2, 2, line_proto("ネコ", (100, 50, 20, 60), 3, 3, [("ネ", (100, 50, 20, 30)), ("コ", (100, 80, 20, 30))]))
    data += field(2, 2, line_proto("OK", (10, 10, 40, 12), 4, 1))
    lines = parse_visual_annotation(data)
    assert [l.text for l in lines] == ["ネコ", "OK"]
    assert lines[0].vertical and not lines[1].vertical
    assert lines[0].block_id == 3 and abs(lines[0].confidence - 0.9) < 1e-6
    assert (lines[0].box.x, lines[0].box.y, lines[0].box.w, lines[0].box.h) == (100, 50, 20, 60)
    assert [t for t, _ in lines[0].symbols] == ["ネ", "コ"]


def test_rotated_box_corners():
    x1, y1, x2, y2 = Box(0, 0, 10, 2, angle=90).corners()
    assert (round(x1), round(y1), round(x2), round(y2)) == (-2, 0, 0, 10)


def test_line_text_folds_dots_and_full_width():
    assert line_text(" う･･･うん ") == "う…うん"
    assert line_text("そんな．．．ＯＫ") == "そんな…OK"


def test_columns_of_one_bubble_are_merged_right_to_left():
    # Two columns of one bubble filed under different blocks, plus a separate bubble further left.
    first, second = vline("それじゃあ", 370, 110, 20, 90, block=6), vline("お別れだ", 347, 110, 20, 75, block=8)
    other = vline("なんで?", 85, 112, 20, 86, block=20)
    out = bubbles([second, other, first])
    texts = sorted(b["text"] for b in out)
    assert texts == ["それじゃあ\nお別れだ", "なんで?"]
    merged = next(b for b in out if "\n" in b["text"])
    assert merged["vertical"] and merged["lines"][0][0] == 370  # right column first


def test_noise_and_furigana_are_dropped():
    page_number = Line("019", Box(65, 1178, 54, 10))
    stray = Line("-D", Box(548, 965, 14, 15))
    column = vline("漢字の読み", 300, 100, 24, 120)
    furigana = vline("かんじ", 326, 100, 10, 40)  # half the width, right beside the column
    out = bubbles([page_number, stray, column, furigana])
    assert [b["text"] for b in out] == ["漢字の読み"]


def test_a_different_size_is_a_different_bubble():
    big = vline("ぜんざいの方か!!", 96, 915, 27, 177, block=8)
    small = vline("そりゃあ", 74, 1012, 13, 55, block=9)
    assert len(bubbles([big, small])) == 2


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("All Screen AI checks passed.")
