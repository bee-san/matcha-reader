#!/usr/bin/env python3
"""Self-check for reading mokuro volumes. No deps -- run: python3 test_mokuro.py"""

from pathlib import Path

from convert_manga import assign_text_to_panels, mokuro_line_text, mokuro_metadata, mokuro_text_blocks
from convert_mokuro_library import Volume, _safe_member, matches, output_paths, safe_name


def block(box, lines, coords, vertical=True):
    return {"box": box, "vertical": vertical, "lines": lines, "lines_coords": coords}


def quad(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def test_metadata_takes_author_from_brackets():
    title, author, series, index = mokuro_metadata({"title": "Aho Girl", "volume": "[ヒロユキ] アホガール 第01巻"})
    assert (title, author, series, index) == ("アホガール 第01巻", "ヒロユキ", "Aho Girl", 1.0)


def test_metadata_full_width_and_western_volume_numbers():
    assert mokuro_metadata({"title": "S", "volume": "よつばと！ 第１２巻"})[3] == 12.0
    assert mokuro_metadata({"title": "S", "volume": "Yotsuba to! v03"})[3] == 3.0
    assert mokuro_metadata({"title": "S", "volume": "Yotsuba to! 07"})[3] == 7.0
    # A number that is part of the title is not a volume number.
    assert mokuro_metadata({"title": "S", "volume": "20世紀少年"})[3] is None
    assert mokuro_metadata({"title": "S", "volume": "［作者］タイトル 第２巻"})[:2] == ("タイトル 第２巻", "作者")
    assert mokuro_metadata({"title": "Series only"})[:3] == ("Series only", "", "Series only")
    assert mokuro_metadata({"title": "Blue Lock", "volume": "\ufffd\ufffd23\ufffd\ufffd"}) == ("Blue Lock 23", "", "Blue Lock", 23.0)


def test_blocks_follow_the_page_through_trim_and_downscale():
    # A 2000x3000 scan, its margin trimmed to (100, 200)-(1900, 2900), then fitted to 480x720.
    record = {"img_width": 2000, "img_height": 3000,
              "blocks": [block([1000, 1100, 1300, 1700], ["一行目", "二行目"],
                               [quad(1200, 1100, 1300, 1700), quad(1000, 1100, 1100, 1500)])]}
    blocks = mokuro_text_blocks(record, (2000, 3000), (100, 200), (1800, 2700), (480, 720))
    assert len(blocks) == 1
    b = blocks[0]
    assert b["box"] == [240, 240, 320, 400]
    assert b["lines"] == [[293, 240, 320, 400], [240, 240, 267, 347]]
    assert b["text"] == "一行目\n二行目"
    assert b["vertical"] is True


def test_measured_size_differs_from_the_file():
    # mokuro ran on a 1000px-wide copy; the image shipped is 2000px wide.
    record = {"img_width": 1000, "img_height": 1500, "blocks": [block([100, 100, 200, 200], ["あ"], [quad(100, 100, 200, 200)])]}
    blocks = mokuro_text_blocks(record, (2000, 3000), (0, 0), (2000, 3000), (480, 720))
    assert blocks[0]["box"] == [48, 48, 96, 96]


def test_lines_are_folded_to_their_printed_cells():
    # One cell for the ellipsis and one for a short Latin run, as the device counts them.
    assert mokuro_line_text(" そんな．．．ＯＫ１００！ ") == "そんな…OK100！"
    assert mokuro_line_text("．．．．．．") == "……"
    record = {"blocks": [block([0, 0, 10, 10], ["え．．．"], [quad(0, 0, 10, 10)])]}
    assert mokuro_text_blocks(record, (10, 10), (0, 0), (10, 10), (10, 10))[0]["text"] == "え…"


def test_text_is_skipped_when_the_image_no_longer_matches():
    record = {"img_width": 1000, "img_height": 1500, "blocks": [block([0, 0, 10, 10], ["あ"], [quad(0, 0, 10, 10)])]}
    assert mokuro_text_blocks(record, (1500, 1000), (0, 0), (1500, 1000), (800, 533)) == []


def test_partial_lines_fall_back_to_the_whole_block():
    # An empty line would shift every later line onto the wrong text segment.
    record = {"blocks": [block([0, 0, 100, 100], ["あ", "", "う"], [quad(0, 0, 10, 10)] * 3)]}
    blocks = mokuro_text_blocks(record, (100, 100), (0, 0), (100, 100), (100, 100))
    assert blocks[0]["lines"] == []
    assert blocks[0]["text"] == "あ\nう"

    record = {"blocks": [block([0, 0, 100, 100], ["あ", "い"], [quad(0, 0, 10, 10)])]}
    assert mokuro_text_blocks(record, (100, 100), (0, 0), (100, 100), (100, 100))[0]["lines"] == []


def test_blocks_outside_the_kept_page_are_dropped():
    record = {"blocks": [block([0, 0, 50, 50], ["ノンブル"], [quad(0, 0, 50, 50)])]}
    assert mokuro_text_blocks(record, (1000, 1000), (100, 100), (800, 800), (800, 800)) == []


def test_text_goes_to_its_panel_in_reading_order():
    right, left = [500, 0, 1000, 500], [0, 0, 480, 500]
    first = {"box": [880, 20, 960, 200]}
    second = {"box": [600, 20, 680, 200]}
    owned = assign_text_to_panels([second, first], [right, left], [right, left])
    assert owned[0] == [first, second]
    assert owned[1] == []


def test_overhanging_bubble_and_margin_text():
    frame = [100, 100, 400, 400]
    crop = [80, 100, 400, 400]  # grown left over a bubble breaching the frame
    bubble = {"box": [82, 150, 98, 200]}  # outside the frame, inside the grown crop
    folio = {"box": [10, 480, 40, 495]}  # page number: in no crop
    owned = assign_text_to_panels([bubble, folio], [frame], [crop])
    assert owned == [[bubble]]


def test_series_filter_and_safe_names():
    assert matches("Aho Girl", ["aho"])
    assert matches("Yotsuba to!", ["yotsuba*"])
    assert not matches("Aho Girl", ["yotsuba"])
    assert matches("Anything", [])
    assert safe_name('Re:Zero? "A/B"') == "Re_Zero_ _A_B_"
    assert safe_name("Vol. ") == "Vol"
    long = safe_name("あ" * 100)
    assert len(long.encode("utf-8")) <= 180 and long == "あ" * 60
    assert not _safe_member("__MACOSX/Vol 1.mokuro") and not _safe_member("Series/._Vol 1.mokuro")
    assert _safe_member("Series/Vol 1.mokuro") and not _safe_member("../Vol 1.mokuro")


def test_colliding_output_names_get_numbered():
    vols = [Volume("S", "Vol 1", Path("a.zip")), Volume("s", "VOL 1", Path("b.cbz")), Volume("S", "Vol?1", Path("c"))]
    paths = output_paths(vols, Path("/out"))
    names = [str(paths[i]) for i in range(3)]
    assert len({n.casefold() for n in names}) == 3
    assert names[0] == "/out/S/Vol 1.matcha" and names[1].endswith("VOL 1 (2).matcha")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("All mokuro checks passed.")
