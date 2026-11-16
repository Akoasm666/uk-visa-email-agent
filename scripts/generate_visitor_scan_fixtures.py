"""Render wholly synthetic passport specimens for the Standard Visitor email test."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def render(font_directory: Path, number: str) -> Image.Image:
    heading = ImageFont.truetype(str(font_directory / "Arial Bold.ttf"), size=79)
    body = ImageFont.truetype(str(font_directory / "Arial.ttf"), size=38)
    image = Image.new("RGB", (1400, 820), (241, 239, 227))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((55, 55, 1340, 765), radius=25, outline=(47, 69, 93), width=5)
    draw.text((110, 105), "PASSPORT", font=heading, fill=(34, 53, 73))
    draw.rounded_rectangle((115, 270, 365, 665), radius=8, fill=(192, 196, 205))
    lines = (
        f"Passport number: {number}",
        "Surname: TESTER",
        "Given names: AVERY",
        "Date of birth: 1991-02-13",
        "Date of expiry: 2031-02-13",
        "Nationality: UTOPIA",
        "SYNTHETIC SPECIMEN",
    )
    for index, line in enumerate(lines):
        draw.text((430, 243 + 63 * index), line, font=body, fill=(26, 37, 47))
    return image


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--fonts", type=Path, default=Path("/System/Library/Fonts/Supplemental"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    clear = render(args.fonts, "ZT1234567")
    blurry = clear.filter(ImageFilter.GaussianBlur(2.8))
    for index, scan in enumerate((blurry, clear), start=1):
        for extension in ("png", "jpg", "pdf"):
            scan.save(args.output / f"scan-{index:02}.{extension}")
    render(args.fonts, "ZT1234599").save(args.output / "scan-03.png")
    Image.new("RGB", clear.size, "white").save(args.output / "scan-04.png")
    clear.crop((50, 50, 1300, 485)).save(args.output / "scan-05.png")


if __name__ == "__main__":
    main()
